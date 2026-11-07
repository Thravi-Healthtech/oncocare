from __future__ import annotations

import base64
import binascii
import html
import json
import math
import mimetypes
import os
import re
import sqlite3
import uuid
from datetime import date, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .analysis import analyse_plan, decision_evidence, grade_lab
from .errors import ApiError
from .database import CLINICIAN, normalise_phone, record_cycle_revision, row_or_none, rows, sync_cycle_summary, transaction, utc_now
from .extraction import ai_status, extract_candidates, save_ai_settings
from . import auth, portal
from .portal import PortalError
from . import radiotherapy, surgery, whatsapp
from . import questionnaire, voice_answers
from . import routes_ext
from http.cookies import SimpleCookie

PORTAL_COOKIE = "oncocare_portal"
from .context import context_status, dailymed_for_drugs, prefetch_async, refresh_dailymed
from . import reference as _reference


def CTCAE_VERSION_NOW() -> str:  # noqa: N802
    return _reference.CTCAE_VERSION
from .reference import CTCAE_TERMS, LAB_CATALOG, SYMPTOM_KEYWORDS, drugs_associated_with, normalise_drug, reference_payload

MAX_JSON = 1_000_000
MAX_UPLOAD_JSON = 15_000_000
OUTCOMES = {"given", "modified", "delayed", "held", "discontinued"}
REASON_CATEGORIES = {"Toxicity", "Laboratory findings", "Clinical status", "Patient preference", "Logistics", "Other"}
PENDING = {"planned", "delayed"}




def require(payload: dict[str, Any], fields: list[str]) -> None:
    missing = [field for field in fields if payload.get(field) in (None, "") or (isinstance(payload.get(field), str) and not payload[field].strip())]
    if missing:
        raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Missing required fields", {"fields": missing})


def require_date_order(payload: dict[str, Any], start: str, end: str) -> None:
    if payload.get(start) and payload.get(end) and payload[end] < payload[start]:
        raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, f"{end} cannot be before {start}")


def validate_dates(payload: dict[str, Any], fields: list[str]) -> None:
    invalid: list[str] = []
    for field in fields:
        value = payload.get(field)
        if value in (None, ""):
            continue
        try:
            date.fromisoformat(str(value))
        except ValueError:
            invalid.append(field)
    if invalid:
        raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Dates must use YYYY-MM-DD format", {"fields": invalid})


def as_number(value: Any, label: str, allow_none: bool = False) -> float | None:
    if value in (None, ""):
        if allow_none:
            return None
        raise ApiError(422, f"{label} is required")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ApiError(422, f"{label} must be a number") from None
    if not math.isfinite(number) or number < 0:
        raise ApiError(422, f"{label} must be a non-negative number")
    return number


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def validate_medications(items: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise ApiError(422, f"{label} needs at least one medication")
    if len(items) > 12:
        raise ApiError(422, f"{label} supports at most 12 medications")
    cleaned = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ApiError(422, f"{label} medication {index} must be an object")
        drug = clean_text(item.get("drug_name"))
        unit = clean_text(item.get("dose_unit"))
        if not drug or not unit:
            raise ApiError(422, f"{label} medication {index} needs a drug name and dose unit")
        dose_value = item.get("planned_dose", item.get("dose"))
        cleaned.append({"id": item.get("id"), "drug_name": drug, "planned_dose": as_number(dose_value, f"{label} {drug} dose"),
                        "dose_unit": unit, "route": clean_text(item.get("route"))})
    names = [m["drug_name"].lower() for m in cleaned]
    if len(set(names)) != len(names):
        raise ApiError(422, f"{label} lists the same drug twice")
    return cleaned


def trend_payload(observations: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(observations, key=lambda item: (item["collection_date"], item["id"]))
    current = ordered[-1]
    previous = ordered[-2] if len(ordered) > 1 else None
    if previous is None or abs(current["value"] - previous["value"]) < 1e-9:
        direction = "stable"
        delta = 0
    else:
        delta = round(current["value"] - previous["value"], 10)
        direction = "up" if delta > 0 else "down"
    abnormal = (
        (current["ref_low"] is not None and current["value"] < current["ref_low"])
        or (current["ref_high"] is not None and current["value"] > current["ref_high"])
    )
    baseline = ordered[0]
    return {
        "test_name": current["test_name"],
        "test_code": current["test_code"],
        "unit": current["unit"],
        "current": current["value"],
        "previous": previous["value"] if previous else None,
        "baseline": baseline["value"],
        "delta": delta,
        "direction": direction,
        "abnormal": abnormal,
        "reference_range": {"low": current["ref_low"], "high": current["ref_high"]},
        "collection_date": current["collection_date"],
        "ctcae": grade_lab(current["test_name"], current["value"], current["unit"], current["ref_low"], current["ref_high"],
                           baseline["value"] if len(ordered) > 1 else None),
        "series": [
            {"value": item["value"], "collection_date": item["collection_date"], "cycle_id": item["cycle_id"]}
            for item in ordered
        ],
    }


def public_mode() -> bool:
    """ONCOCARE_PUBLIC=1 when the server is exposed on the internet behind HTTPS (e.g. onco.thravi.in)."""
    return os.environ.get("ONCOCARE_PUBLIC", "").lower() in {"1", "true", "yes"}


def cookie_attrs() -> str:
    return "HttpOnly; SameSite=Strict" + ("; Secure" if public_mode() else "")


def demo_banner_html() -> bytes:
    text = os.environ.get("ONCOCARE_DEMO_BANNER", "").strip()
    if not text:
        return b""
    safe = html.escape(text)
    return (f'<div role="note" style="background:#7a2e00;color:#fff;text-align:center;padding:6px 12px;'
            f'font:600 13px/1.4 system-ui,sans-serif;position:relative;z-index:9999">{safe}</div>').encode()


def make_handler(db_path: str | Path, static_dir: str | Path | None = None, require_auth: bool = True) -> type[BaseHTTPRequestHandler]:
    database_path = str(db_path)
    upload_dir = Path(database_path).resolve().parent / "uploads" if database_path != ":memory:" else Path("uploads")
    whatsapp.MEDIA_DIR = upload_dir / "whatsapp"
    web_root = Path(static_dir).resolve() if static_dir else None

    class Handler(BaseHTTPRequestHandler):
        server_version = "ThraviOncoCare/0.2"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self._cors_headers()
            self.end_headers()

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PATCH(self) -> None:
            self._dispatch("PATCH")

        def do_PUT(self) -> None:
            self._dispatch("PATCH")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/") and parsed.path != "/health":
                return self._serve_static(parsed.path)
            self._extra_headers = []
            self.clinician = None
            try:
                if parsed.path == "/api/whatsapp/webhook":
                    return self._whatsapp_webhook(method, parsed)
                if parsed.path.startswith("/api/auth/"):
                    result, status = self._auth_route(method, parsed.path)
                    return self._json(status, result)
                if parsed.path.startswith("/api/") and not parsed.path.startswith("/api/portal/"):
                    with transaction(database_path) as connection:
                        self.clinician = auth.session_clinician(connection, self._cookie(auth.DOCTOR_COOKIE))
                    if require_auth and not self.clinician:
                        raise ApiError(HTTPStatus.UNAUTHORIZED, "Please sign in.")
                file_match = re.fullmatch(r"/api/lab-documents/(\d+)/file", parsed.path)
                if file_match and method == "GET":
                    return self._serve_document(int(file_match.group(1)))
                binary = self._binary_route(method, parsed.path)
                if isinstance(binary, routes_ext.RawResponse):
                    return self._send_raw(binary)
                if binary:
                    return self._send_file(*binary)
                if parsed.path.startswith("/api/portal/"):
                    result, status = self._portal_route(method, parsed.path)
                else:
                    routed = self._route(method, parsed.path, parse_qs(parsed.query))
                    if isinstance(routed, routes_ext.RawResponse):
                        return self._send_raw(routed)
                    result, status = routed
                self._json(status, result)
            except auth.AuthError as error:
                self._json(error.status, {"error": {"message": error.message, "details": {}}})
            except PortalError as error:
                self._json(error.status, {"error": {"message": error.message, "details": {}}})
            except ApiError as error:
                self._json(error.status, {"error": {"message": error.message, "details": error.details}})
            except sqlite3.IntegrityError as error:
                self._json(HTTPStatus.CONFLICT, {"error": {"message": "Database constraint failed", "details": {"reason": str(error)}}})
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": {"message": "Request body must be valid JSON"}})
            except Exception as error:  # pragma: no cover - defensive
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": "Unexpected server error", "details": {} if public_mode() else {"type": type(error).__name__, "reason": str(error)}}})

        # ------------------------------------------------------------------ routing
        def _route(self, method: str, path: str, query: dict[str, list[str]]) -> tuple[dict[str, Any], int]:
            if method == "GET" and path == "/health":
                return {"status": "ok", "service": "thravi-oncocare-api", "api_version": 3, "ai_extraction": ai_status(), "context": context_status()}, HTTPStatus.OK
            if method == "GET" and path == "/api/reference":
                return {"data": reference_payload()}, HTTPStatus.OK
            if path == "/api/settings/ai" and method == "GET":
                return {"data": ai_status()}, HTTPStatus.OK
            if path == "/api/settings/ai" and method == "POST":
                if public_mode():
                    raise ApiError(HTTPStatus.FORBIDDEN, "AI settings are managed by the server administrator on this deployment.")
                body = self._body()
                try:
                    return {"data": save_ai_settings(body.get("api_key"), body.get("model"))}, HTTPStatus.OK
                except ValueError as error:
                    raise ApiError(422, str(error)) from None
                except RuntimeError as error:
                    raise ApiError(502, str(error)) from None
            if path == "/api/context" and method == "GET":
                return {"data": context_status()}, HTTPStatus.OK
            if path == "/api/context/dailymed/refresh" and method == "POST":
                body = self._body()
                drugs = [d for d in body.get("drugs", []) if isinstance(d, str) and d.strip()][:12]
                if not drugs:
                    raise ApiError(422, "Provide drugs to refresh")
                return {"data": refresh_dailymed(drugs)}, HTTPStatus.OK
            if path == "/api/context/drug-terms" and method == "GET":
                names = [d for d in query.get("drugs", [""])[0].split(",") if d.strip()]
                labels = dailymed_for_drugs(names)
                return {"data": {d: {"source": "DailyMed" if d in labels else "curated shortlist",
                                     "terms": sorted(labels[d]["terms"]) if d in labels else
                                     (_reference.DRUG_TOXICITIES.get(_reference.normalise_drug(d) or "", {}).get("terms", []))} for d in names}}, HTTPStatus.OK
            if method == "GET" and path == "/api/ctcae/suggest":
                return self._suggest_terms(query), HTTPStatus.OK
            extended = routes_ext.dispatch(routes_ext.Ctx(database_path, self._body, self._clinician_name(), upload_dir, query), method, path)
            if extended is not None:
                return extended
            routes: list[tuple[str, dict[str, Any]]] = [
                (r"/api/patients", {"GET": lambda: self._list_patients(query), "POST": lambda: (self._create_patient(), 201)}),
                (r"/api/patients/(\d+)", {"GET": self._get_patient, "PATCH": self._update_patient, "DELETE": lambda i: self._delete("patients", i)}),
                (r"/api/patients/(\d+)/treatment-plans", {"GET": self._list_plans, "POST": lambda i: (self._create_plan(i), 201)}),
                (r"/api/patients/(\d+)/lab-trends", {"GET": lambda i: self._lab_trends(i, query)}),
                (r"/api/patients/(\d+)/symptoms", {"GET": lambda i: self._list_symptoms(i, query), "POST": lambda i: (self._create_symptom(i), 201)}),
                (r"/api/treatment-plans/(\d+)", {"GET": self._get_plan, "PATCH": self._update_plan, "DELETE": lambda i: self._delete("treatment_plans", i)}),
                (r"/api/treatment-plans/(\d+)/cycles", {"GET": self._list_cycles, "POST": lambda i: (self._create_cycle(i), 201)}),
                (r"/api/treatment-plans/(\d+)/longitudinal", {"GET": self._longitudinal}),
                (r"/api/cycles/(\d+)", {"GET": self._get_cycle, "PATCH": self._update_cycle, "DELETE": lambda i: self._delete("treatment_cycles", i)}),
                (r"/api/cycles/(\d+)/record", {"POST": self._record_cycle}),
                (r"/api/cycles/(\d+)/revisions", {"GET": self._cycle_revisions}),
                (r"/api/cycles/(\d+)/decisions", {"GET": self._cycle_decisions}),
                (r"/api/cycles/(\d+)/labs", {"GET": self._list_labs, "POST": lambda i: (self._create_labs(i), 201)}),
                (r"/api/cycles/(\d+)/lab-documents", {"GET": self._list_documents, "POST": lambda i: (self._upload_document(i), 201)}),
                (r"/api/labs/(\d+)", {"GET": lambda i: self._get_record("lab_investigations", i), "PATCH": self._update_lab, "DELETE": lambda i: self._delete("lab_investigations", i)}),
                (r"/api/labs/(\d+)/verify", {"POST": self._verify_lab}),
                (r"/api/patients/(\d+)/portal", {"GET": self._portal_status, "POST": self._portal_enable, "DELETE": self._portal_disable}),
                (r"/api/patients/(\d+)/patient-reports", {"GET": lambda i: self._patient_reports(i, query)}),
                (r"/api/patient-reports", {"GET": lambda: self._patient_reports(None, query)}),
                (r"/api/patient-reports/(\d+)", {"GET": self._patient_report, "PATCH": self._review_patient_report}),
                (r"/api/patient-reports/(\d+)/triage", {"POST": lambda i: {"data": portal.retriage(database_path, i)}}),
                (r"/api/symptoms/(\d+)", {"GET": lambda i: self._get_record("symptoms", i), "PATCH": self._update_symptom, "DELETE": lambda i: self._delete("symptoms", i)}),
            ]
            for pattern, handlers in routes:
                match = re.fullmatch(pattern, path)
                if not match:
                    continue
                handler = handlers.get(method)
                if handler is None:
                    raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
                result = handler(*(int(group) for group in match.groups()))
                if isinstance(result, tuple):
                    return result
                return result, HTTPStatus.OK
            raise ApiError(HTTPStatus.NOT_FOUND, "Endpoint not found")

        def _body(self, limit: int = MAX_JSON) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length > limit:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"Request body exceeds {limit // 1_000_000} MB")
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object")
            return payload

        # ------------------------------------------------------------------ patients
        def _list_patients(self, query: dict[str, list[str]]) -> dict[str, Any]:
            search = query.get("search", [""])[0].strip()
            status = query.get("status", ["all"])[0]
            where, params = [], []
            if search:
                pattern = f"%{search}%"
                where.append("(p.medical_record_number LIKE ? OR p.first_name LIKE ? OR p.last_name LIKE ? OR (p.first_name || ' ' || p.last_name) LIKE ? OR p.diagnosis LIKE ? OR p.phone LIKE ?)")
                digits = normalise_phone(search) if re.fullmatch(r"[\d\s+()-]{4,}", search) else None
                params += [pattern] * 5 + [f"%{digits}%" if digits else "\u0000"]
            if status in ("active", "inactive"):
                where.append("p.active = ?")
                params.append(1 if status == "active" else 0)
            sql = f"""
                SELECT p.*,
                  (SELECT COUNT(*) FROM treatment_plans tp WHERE tp.patient_id = p.id) AS plan_count,
                  (SELECT tp.regimen_name FROM treatment_plans tp WHERE tp.patient_id = p.id AND tp.status = 'active' ORDER BY tp.start_date DESC LIMIT 1) AS active_regimen,
                  (SELECT c.planned_date FROM treatment_cycles c JOIN treatment_plans tp ON tp.id = c.treatment_plan_id
                     WHERE tp.patient_id = p.id AND tp.status = 'active' AND c.status IN ('planned','delayed') ORDER BY c.planned_date LIMIT 1) AS next_cycle_date,
                  (SELECT c.cycle_number FROM treatment_cycles c JOIN treatment_plans tp ON tp.id = c.treatment_plan_id
                     WHERE tp.patient_id = p.id AND tp.status = 'active' AND c.status IN ('planned','delayed') ORDER BY c.planned_date LIMIT 1) AS next_cycle_number,
                  MAX(COALESCE((SELECT MAX(s.grade) FROM symptoms s WHERE s.patient_id = p.id AND s.status != 'resolved'), 0),
                      COALESCE((SELECT MAX(i.grade) FROM rt_review_items i WHERE i.review_id = (
                          SELECT r.id FROM rt_reviews r JOIN rt_courses rc ON rc.id = r.course_id
                          WHERE rc.patient_id = p.id AND rc.status IN ('active', 'on_hold') ORDER BY r.review_date DESC, r.id DESC LIMIT 1)), 0)) AS open_toxicity_grade,
                  (SELECT rc.site FROM rt_courses rc WHERE rc.patient_id = p.id AND rc.status IN ('planned', 'active', 'on_hold') ORDER BY rc.id DESC LIMIT 1) AS active_rt_site,
                  (SELECT sc.procedure_planned FROM surgical_cases sc WHERE sc.patient_id = p.id AND sc.status IN ('planned', 'scheduled', 'completed') ORDER BY sc.id DESC LIMIT 1) AS active_surgery,
                  (SELECT sc.surgery_date FROM surgical_cases sc WHERE sc.patient_id = p.id AND sc.status IN ('planned', 'scheduled') ORDER BY sc.surgery_date LIMIT 1) AS next_surgery_date,
                  (SELECT sc.status FROM surgical_cases sc WHERE sc.patient_id = p.id AND sc.status IN ('planned', 'scheduled', 'completed') ORDER BY sc.id DESC LIMIT 1) AS active_surgery_status,
                  (SELECT rc.id FROM rt_courses rc WHERE rc.patient_id = p.id AND rc.status IN ('planned', 'active', 'on_hold') ORDER BY rc.id DESC LIMIT 1) AS active_rt_id,
                  (SELECT COUNT(*) FROM rt_fractions f JOIN rt_courses rc ON rc.id = f.course_id WHERE rc.patient_id = p.id AND rc.status IN ('planned', 'active', 'on_hold')) AS active_rt_fractions,
                  (SELECT SUM(ph.fractions_planned) FROM rt_phases ph JOIN rt_courses rc ON rc.id = ph.course_id WHERE rc.patient_id = p.id AND rc.status IN ('planned', 'active', 'on_hold')) AS active_rt_planned,
                  (SELECT COUNT(*) FROM patient_reports r WHERE r.patient_id = p.id AND r.status = 'new') AS open_reports,
                  (SELECT COUNT(*) FROM patient_reports r WHERE r.patient_id = p.id AND r.status = 'new' AND r.red_flags_json NOT IN ('[]', '')) AS urgent_reports
                FROM patients p {('WHERE ' + ' AND '.join(where)) if where else ''}
                ORDER BY p.active DESC, p.last_name, p.first_name"""
            with transaction(database_path) as connection:
                data = rows(connection, sql, tuple(params))
            return {"data": data, "count": len(data)}

        def _patient_payload(self, payload: dict[str, Any], partial: bool) -> dict[str, Any]:
            if not partial:
                require(payload, ["medical_record_number", "first_name", "last_name"])
            validate_dates(payload, ["date_of_birth"])
            if payload.get("date_of_birth") and payload["date_of_birth"] > date.today().isoformat():
                raise ApiError(422, "Date of birth cannot be in the future")
            cleaned: dict[str, Any] = {}
            for key in ("medical_record_number", "first_name", "last_name", "date_of_birth", "sex", "diagnosis"):
                if key in payload:
                    cleaned[key] = clean_text(payload[key])
                    if key in ("medical_record_number", "first_name", "last_name") and not cleaned[key]:
                        raise ApiError(422, "Missing required fields", {"fields": [key]})
            if "active" in payload:
                cleaned["active"] = 1 if payload["active"] in (True, 1, "1", "true", "active") else 0
            if cleaned.get("sex") and cleaned["sex"] not in ("female", "male", "other"):
                raise ApiError(422, "sex must be female, male or other")
            if "phone" in payload:
                phone = normalise_phone(payload["phone"])
                if phone and not 8 <= len(phone) <= 15:
                    raise ApiError(422, "Enter a valid mobile number (10 digits for India)", {"fields": ["phone"]})
                cleaned["phone"] = phone
            return cleaned

        def _create_patient(self) -> dict[str, Any]:
            cleaned = self._patient_payload(self._body(), partial=False)
            now = utc_now()
            with transaction(database_path) as connection:
                patient_id = connection.execute(
                    """INSERT INTO patients (medical_record_number, first_name, last_name, date_of_birth, sex, diagnosis, phone, active, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (cleaned["medical_record_number"], cleaned["first_name"], cleaned["last_name"], cleaned.get("date_of_birth"),
                     cleaned.get("sex"), cleaned.get("diagnosis"), cleaned.get("phone"), cleaned.get("active", 1), now, now),
                ).lastrowid
                patient = row_or_none(connection, "SELECT * FROM patients WHERE id = ?", (patient_id,))
            return {"data": patient}

        def _get_patient(self, patient_id: int) -> dict[str, Any]:
            with transaction(database_path) as connection:
                patient = row_or_none(connection, "SELECT * FROM patients WHERE id = ?", (patient_id,))
                if not patient:
                    raise ApiError(HTTPStatus.NOT_FOUND, "Patient not found")
                patient["treatment_plans"] = self._plans_with_summary(connection, patient_id)
                patient["rt_courses"] = radiotherapy.list_for_patient(connection, patient_id)
                patient["surgeries"] = surgery.list_for_patient(connection, patient_id)
            return {"data": patient}

        def _plans_with_summary(self, connection: sqlite3.Connection, patient_id: int) -> list[dict[str, Any]]:
            plans = rows(connection, "SELECT * FROM treatment_plans WHERE patient_id = ? ORDER BY start_date DESC, id DESC", (patient_id,))
            for plan in plans:
                plan["medications"] = rows(connection, "SELECT * FROM plan_medications WHERE treatment_plan_id = ? ORDER BY sort_order, id", (plan["id"],))
                counts = row_or_none(connection, """SELECT COUNT(*) AS total,
                        SUM(CASE WHEN status IN ('given','modified') THEN 1 ELSE 0 END) AS given,
                        SUM(CASE WHEN status = 'modified' THEN 1 ELSE 0 END) AS modified
                        FROM treatment_cycles WHERE treatment_plan_id = ?""", (plan["id"],))
                plan["cycle_counts"] = {k: counts[k] or 0 for k in ("total", "given", "modified")}
                plan["next_cycle"] = row_or_none(connection, """SELECT id, cycle_number, planned_date, status FROM treatment_cycles
                        WHERE treatment_plan_id = ? AND status IN ('planned','delayed') ORDER BY cycle_number LIMIT 1""", (plan["id"],))
            return plans

        def _update_patient(self, patient_id: int) -> dict[str, Any]:
            cleaned = self._patient_payload(self._body(), partial=True)
            return self._update_record("patients", patient_id, cleaned, list(cleaned))

        # ------------------------------------------------------------------ plans
        def _list_plans(self, patient_id: int) -> dict[str, Any]:
            self._ensure("patients", patient_id)
            with transaction(database_path) as connection:
                data = self._plans_with_summary(connection, patient_id)
            return {"data": data, "count": len(data)}

        def _get_plan(self, plan_id: int) -> dict[str, Any]:
            with transaction(database_path) as connection:
                plan = row_or_none(connection, "SELECT * FROM treatment_plans WHERE id = ?", (plan_id,))
                if not plan:
                    raise ApiError(404, "Treatment plan not found")
                plan["medications"] = rows(connection, "SELECT * FROM plan_medications WHERE treatment_plan_id = ? ORDER BY sort_order, id", (plan_id,))
            return {"data": plan}

        def _create_plan(self, patient_id: int) -> dict[str, Any]:
            self._ensure("patients", patient_id)
            payload = self._body()
            require(payload, ["regimen_name"])
            validate_dates(payload, ["start_date"])
            now = utc_now()
            cycle_specs = payload.get("cycles", [])
            if not isinstance(cycle_specs, list) or len(cycle_specs) > 24:
                raise ApiError(422, "cycles must be a list of at most 24 entries")
            plan_meds = validate_medications(payload["medications"], "Plan") if payload.get("medications") else []
            validated_cycles = []
            seen_numbers: set[int] = set()
            for index, cycle in enumerate(cycle_specs, start=1):
                if not isinstance(cycle, dict):
                    raise ApiError(422, f"Cycle {index} must be an object")
                require(cycle, ["cycle_number", "planned_date"])
                validate_dates(cycle, ["planned_date"])
                try:
                    number = int(cycle["cycle_number"])
                except (TypeError, ValueError):
                    raise ApiError(422, f"Cycle {index} has an invalid number") from None
                if number < 1 or number in seen_numbers:
                    raise ApiError(422, f"Cycle {index} has a duplicate/invalid number")
                if "medications" in cycle:
                    meds = validate_medications(cycle["medications"], f"Cycle {number}")
                else:  # legacy single-drug payload
                    meds = validate_medications([{k: cycle.get(k) for k in ("drug_name", "planned_dose", "dose_unit", "route")}], f"Cycle {number}")
                seen_numbers.add(number)
                validated_cycles.append((number, cycle["planned_date"], meds))
            if not plan_meds and validated_cycles:
                plan_meds = [dict(m) for m in validated_cycles[0][2]]
            status = payload.get("status") or "active"
            if status not in ("active", "planned", "completed", "on_hold", "discontinued"):
                raise ApiError(422, "Invalid plan status")
            interval = payload.get("cycle_interval_days")
            with transaction(database_path) as connection:
                plan_id = connection.execute(
                    """INSERT INTO treatment_plans (patient_id, regimen_name, intent, status, start_date, planned_cycles,
                           cycle_interval_days, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (patient_id, clean_text(payload["regimen_name"]), clean_text(payload.get("intent")), status, payload.get("start_date"),
                     len(validated_cycles) or payload.get("planned_cycles"), int(interval) if interval else None, clean_text(payload.get("notes")), now, now),
                ).lastrowid
                for order, med in enumerate(plan_meds):
                    connection.execute(
                        "INSERT INTO plan_medications (treatment_plan_id, drug_name, dose, dose_unit, route, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                        (plan_id, med["drug_name"], med["planned_dose"], med["dose_unit"], med["route"], order),
                    )
                for number, planned_date, meds in validated_cycles:
                    self._insert_cycle(connection, plan_id, number, planned_date, meds, now)
                plan = row_or_none(connection, "SELECT * FROM treatment_plans WHERE id = ?", (plan_id,))
                plan["medications"] = rows(connection, "SELECT * FROM plan_medications WHERE treatment_plan_id = ? ORDER BY sort_order", (plan_id,))
            if payload.get("notify_whatsapp"):
                plan["whatsapp"] = [whatsapp.notify(database_path, patient_id, "plan", self._clinician_name())]
                first = validated_cycles[0] if validated_cycles else None
                if first and first[1] > date.today().isoformat():
                    plan["whatsapp"].append(whatsapp.notify(database_path, patient_id, "reminder", self._clinician_name(), visit_date=first[1],
                                                            visit_time="09:00", purpose=f"{clean_text(payload['regimen_name'])}, cycle {first[0]}"))
            return {"data": plan}

        def _insert_cycle(self, connection: sqlite3.Connection, plan_id: int, number: int, planned_date: str,
                          meds: list[dict[str, Any]], now: str) -> int:
            first = meds[0]
            cycle_id = connection.execute(
                """INSERT INTO treatment_cycles (treatment_plan_id, cycle_number, planned_date, status, drug_name, planned_dose,
                       dose_unit, route, created_at, updated_at) VALUES (?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?)""",
                (plan_id, number, planned_date, first["drug_name"], first["planned_dose"], first["dose_unit"], first["route"], now, now),
            ).lastrowid
            for order, med in enumerate(meds):
                connection.execute(
                    """INSERT INTO cycle_medications (cycle_id, drug_name, planned_dose, dose_unit, route, status, sort_order)
                       VALUES (?, ?, ?, ?, ?, 'planned', ?)""",
                    (cycle_id, med["drug_name"], med["planned_dose"], med["dose_unit"], med["route"], order),
                )
            sync_cycle_summary(connection, cycle_id)
            return cycle_id

        def _update_plan(self, plan_id: int) -> dict[str, Any]:
            payload = self._body()
            validate_dates(payload, ["start_date"])
            if "regimen_name" in payload and not clean_text(payload["regimen_name"]):
                raise ApiError(422, "Missing required fields", {"fields": ["regimen_name"]})
            if "status" in payload and payload["status"] not in ("active", "planned", "completed", "on_hold", "discontinued"):
                raise ApiError(422, "Invalid plan status")
            result = self._update_record("treatment_plans", plan_id, payload, ["regimen_name", "intent", "status", "start_date", "notes"])
            if "medications" in payload:
                meds = validate_medications(payload["medications"], "Plan")
                with transaction(database_path) as connection:
                    connection.execute("DELETE FROM plan_medications WHERE treatment_plan_id = ?", (plan_id,))
                    for order, med in enumerate(meds):
                        connection.execute(
                            "INSERT INTO plan_medications (treatment_plan_id, drug_name, dose, dose_unit, route, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                            (plan_id, med["drug_name"], med["planned_dose"], med["dose_unit"], med["route"], order),
                        )
            return self._get_plan(plan_id) if result else result

        # ------------------------------------------------------------------ cycles
        def _cycle_with_meds(self, connection: sqlite3.Connection, cycle_id: int) -> dict[str, Any] | None:
            cycle = row_or_none(connection, "SELECT * FROM treatment_cycles WHERE id = ?", (cycle_id,))
            if cycle:
                cycle["medications"] = rows(connection, "SELECT * FROM cycle_medications WHERE cycle_id = ? ORDER BY sort_order, id", (cycle_id,))
            return cycle

        def _list_cycles(self, plan_id: int) -> dict[str, Any]:
            self._ensure("treatment_plans", plan_id)
            with transaction(database_path) as connection:
                ids = rows(connection, "SELECT id FROM treatment_cycles WHERE treatment_plan_id = ? ORDER BY cycle_number", (plan_id,))
                data = [self._cycle_with_meds(connection, row["id"]) for row in ids]
            return {"data": data, "count": len(data)}

        def _create_cycle(self, plan_id: int) -> dict[str, Any]:
            self._ensure("treatment_plans", plan_id)
            payload = self._body()
            require(payload, ["cycle_number", "planned_date"])
            validate_dates(payload, ["planned_date", "administered_date"])
            try:
                number = int(payload["cycle_number"])
            except (TypeError, ValueError):
                raise ApiError(422, "cycle_number must be a positive integer") from None
            if number < 1:
                raise ApiError(422, "cycle_number must be positive and planned_dose non-negative")
            if "medications" in payload:
                meds = validate_medications(payload["medications"], f"Cycle {number}")
            else:
                if payload.get("planned_dose") not in (None, "") and float(payload["planned_dose"]) < 0:
                    raise ApiError(422, "cycle_number must be positive and planned_dose non-negative")
                meds = validate_medications([{"drug_name": payload.get("drug_name"), "planned_dose": payload.get("planned_dose"),
                                              "dose_unit": payload.get("dose_unit") or "mg/m2", "route": payload.get("route")}], f"Cycle {number}")
            now = utc_now()
            with transaction(database_path) as connection:
                cycle_id = self._insert_cycle(connection, plan_id, number, payload["planned_date"], meds, now)
                connection.execute("UPDATE treatment_plans SET planned_cycles = (SELECT COUNT(*) FROM treatment_cycles WHERE treatment_plan_id = ?) WHERE id = ?", (plan_id, plan_id))
                cycle = self._cycle_with_meds(connection, cycle_id)
            return {"data": cycle}

        def _get_cycle(self, cycle_id: int) -> dict[str, Any]:
            with transaction(database_path) as connection:
                cycle = self._cycle_with_meds(connection, cycle_id)
                if not cycle:
                    raise ApiError(HTTPStatus.NOT_FOUND, "Treatment cycle not found")
                cycle["labs"] = rows(connection, "SELECT * FROM lab_investigations WHERE cycle_id = ? ORDER BY collection_date, test_name", (cycle_id,))
                cycle["symptoms"] = rows(connection, "SELECT * FROM symptoms WHERE cycle_id = ? ORDER BY onset_date, id", (cycle_id,))
                cycle["decisions"] = self._decisions(connection, cycle_id)
            return {"data": cycle}

        def _update_cycle(self, cycle_id: int) -> dict[str, Any]:
            """Edit planned values (date, drugs, doses). Outcomes are recorded through /record."""
            payload = self._body()
            validate_dates(payload, ["planned_date", "administered_date"])
            allowed = ["cycle_number", "planned_date", "administered_date", "status", "modification_reason", "reason_category"]
            if "cycle_number" in payload and int(payload["cycle_number"]) < 1:
                raise ApiError(422, "cycle_number must be positive")
            legacy = {k: payload[k] for k in ("drug_name", "planned_dose", "actual_dose", "dose_unit", "route") if k in payload}
            for key in ("planned_dose", "actual_dose"):
                if key in legacy and legacy[key] is not None and float(legacy[key]) < 0:
                    raise ApiError(422, f"{key} must be non-negative")
            if "status" in payload and payload["status"] not in ("planned", "given", "modified", "delayed", "held", "discontinued", "cancelled", "administered", "completed"):
                raise ApiError(422, "Invalid cycle status")
            if payload.get("status") in ("administered", "completed"):
                payload["status"] = "given"
            meds = validate_medications(payload["medications"], "Cycle") if "medications" in payload else None
            with transaction(database_path) as connection:
                cycle = self._cycle_with_meds(connection, cycle_id)
                if not cycle:
                    raise ApiError(404, "Treatment cycle not found")
                record_cycle_revision(connection, cycle_id)
                fields = [f for f in allowed if f in payload]
                if fields:
                    self._apply_update(connection, "treatment_cycles", cycle_id, payload, fields)
                if meds is not None:
                    existing = {m["id"]: m for m in cycle["medications"]}
                    keep = []
                    for order, med in enumerate(meds):
                        if med.get("id") in existing:
                            connection.execute(
                                "UPDATE cycle_medications SET drug_name = ?, planned_dose = ?, dose_unit = ?, route = ?, sort_order = ? WHERE id = ?",
                                (med["drug_name"], med["planned_dose"], med["dose_unit"], med["route"], order, med["id"]))
                            keep.append(med["id"])
                        else:
                            keep.append(connection.execute(
                                """INSERT INTO cycle_medications (cycle_id, drug_name, planned_dose, dose_unit, route, status, sort_order)
                                   VALUES (?, ?, ?, ?, ?, 'planned', ?)""",
                                (cycle_id, med["drug_name"], med["planned_dose"], med["dose_unit"], med["route"], order)).lastrowid)
                    connection.execute(f"DELETE FROM cycle_medications WHERE cycle_id = ? AND id NOT IN ({','.join('?' * len(keep))})", (cycle_id, *keep))
                elif legacy:
                    first = cycle["medications"][0] if cycle["medications"] else None
                    if first:
                        updates = {"drug_name": legacy.get("drug_name", first["drug_name"]),
                                   "planned_dose": legacy.get("planned_dose", first["planned_dose"]),
                                   "actual_dose": legacy.get("actual_dose", first["actual_dose"]),
                                   "dose_unit": legacy.get("dose_unit", first["dose_unit"]), "route": legacy.get("route", first["route"])}
                        status = payload.get("status", cycle["status"])
                        med_status = first["status"]
                        if status in ("given", "modified"):
                            med_status = "given" if updates["actual_dose"] is not None and abs(updates["actual_dose"] - updates["planned_dose"]) < 1e-9 else "modified"
                        elif status == "held":
                            med_status = "held"
                        connection.execute(
                            "UPDATE cycle_medications SET drug_name = ?, planned_dose = ?, actual_dose = ?, dose_unit = ?, route = ?, status = ? WHERE id = ?",
                            (updates["drug_name"], updates["planned_dose"], updates["actual_dose"], updates["dose_unit"], updates["route"], med_status, first["id"]))
                if not fields and meds is None and not legacy:
                    raise ApiError(422, "No supported fields supplied")
                connection.execute("UPDATE treatment_cycles SET updated_at = ? WHERE id = ?", (utc_now(), cycle_id))
                sync_cycle_summary(connection, cycle_id)
                updated = self._cycle_with_meds(connection, cycle_id)
            return {"data": updated, "meta": {"previous_version_preserved": True}}

        def _record_cycle(self, cycle_id: int) -> dict[str, Any]:
            """Record what happened to a cycle: given as planned, modified, delayed, held or discontinued."""
            payload = self._body()
            outcome = payload.get("outcome")
            if outcome not in OUTCOMES:
                raise ApiError(422, "outcome must be one of: " + ", ".join(sorted(OUTCOMES)))
            validate_dates(payload, ["administered_date", "new_planned_date"])
            rationale = clean_text(payload.get("rationale"))
            category = clean_text(payload.get("reason_category"))
            recorded_by = clean_text(payload.get("recorded_by")) or self._clinician_name()
            if category and category not in REASON_CATEGORIES:
                raise ApiError(422, "Unknown reason category")
            if outcome != "given" and (not rationale or not category):
                raise ApiError(422, "A reason category and clinical rationale are required when a cycle is not given as planned",
                               {"fields": [f for f, v in (("reason_category", category), ("rationale", rationale)) if not v]})
            with transaction(database_path) as connection:
                cycle = self._cycle_with_meds(connection, cycle_id)
                if not cycle:
                    raise ApiError(404, "Treatment cycle not found")
                plan = row_or_none(connection, "SELECT * FROM treatment_plans WHERE id = ?", (cycle["treatment_plan_id"],))
                meds_by_id = {m["id"]: m for m in cycle["medications"]}
                med_updates: dict[int, tuple[float | None, str]] = {}
                if outcome in ("given", "modified"):
                    if not payload.get("administered_date"):
                        raise ApiError(422, "Administration date is required when the cycle was given", {"fields": ["administered_date"]})
                    if payload["administered_date"] > date.today().isoformat():
                        raise ApiError(422, "Administration date cannot be in the future")
                    supplied = {int(m["id"]): m for m in payload.get("medications", []) if isinstance(m, dict) and m.get("id") is not None}
                    unknown = set(supplied) - set(meds_by_id)
                    if unknown:
                        raise ApiError(422, "Medication does not belong to this cycle")
                    for med_id, med in meds_by_id.items():
                        item = supplied.get(med_id, {})
                        held = item.get("status") == "held"
                        actual = None if held else as_number(item.get("actual_dose", med["planned_dose"]), f"{med['drug_name']} actual dose")
                        if held or actual == 0:
                            med_updates[med_id] = (None, "held")
                        elif abs(actual - med["planned_dose"]) > 1e-9:
                            med_updates[med_id] = (actual, "modified")
                        else:
                            med_updates[med_id] = (actual, "given")
                    changed = [meds_by_id[i]["drug_name"] for i, (_, s) in med_updates.items() if s != "given"]
                    if all(s == "held" for _, s in med_updates.values()):
                        raise ApiError(422, "All drugs are held; record the cycle as held instead")
                    if outcome == "given" and changed:
                        raise ApiError(422, f"Doses differ from the plan for {', '.join(changed)}. Record the cycle as modified with a reason.",
                                       {"changed": changed})
                    if outcome == "modified" and not changed and payload.get("administered_date") == cycle["planned_date"]:
                        raise ApiError(422, "No dose or drug change recorded. Change a dose, hold a drug, or record as given.")
                elif outcome == "delayed":
                    new_date = payload.get("new_planned_date")
                    if not new_date:
                        raise ApiError(422, "New planned date is required for a delay", {"fields": ["new_planned_date"]})
                    if new_date <= cycle["planned_date"]:
                        raise ApiError(422, "New planned date must be after the current planned date")
                record_cycle_revision(connection, cycle_id)
                now = utc_now()
                if outcome in ("given", "modified"):
                    for med_id, (actual, status) in med_updates.items():
                        connection.execute("UPDATE cycle_medications SET actual_dose = ?, status = ? WHERE id = ?", (actual, status, med_id))
                    connection.execute(
                        """UPDATE treatment_cycles SET status = ?, administered_date = ?, modification_reason = ?, reason_category = ?,
                               recorded_by = ?, recorded_at = ?, updated_at = ? WHERE id = ?""",
                        (outcome, payload["administered_date"], rationale, category, recorded_by, now, now, cycle_id))
                elif outcome == "delayed":
                    shift = (date.fromisoformat(payload["new_planned_date"]) - date.fromisoformat(cycle["planned_date"])).days
                    connection.execute(
                        """UPDATE treatment_cycles SET status = 'delayed', planned_date = ?, modification_reason = ?, reason_category = ?,
                               recorded_by = ?, recorded_at = ?, updated_at = ? WHERE id = ?""",
                        (payload["new_planned_date"], rationale, category, recorded_by, now, now, cycle_id))
                    if payload.get("shift_following"):
                        later = rows(connection, "SELECT id, planned_date FROM treatment_cycles WHERE treatment_plan_id = ? AND cycle_number > ? AND status IN ('planned','delayed')",
                                     (cycle["treatment_plan_id"], cycle["cycle_number"]))
                        for item in later:
                            record_cycle_revision(connection, item["id"])
                            shifted = (date.fromisoformat(item["planned_date"]) + timedelta(days=shift)).isoformat()
                            connection.execute("UPDATE treatment_cycles SET planned_date = ?, updated_at = ? WHERE id = ?", (shifted, now, item["id"]))
                elif outcome == "held":
                    connection.execute("UPDATE cycle_medications SET status = 'held', actual_dose = NULL WHERE cycle_id = ?", (cycle_id,))
                    connection.execute(
                        """UPDATE treatment_cycles SET status = 'held', administered_date = NULL, modification_reason = ?, reason_category = ?,
                               recorded_by = ?, recorded_at = ?, updated_at = ? WHERE id = ?""",
                        (rationale, category, recorded_by, now, now, cycle_id))
                elif outcome == "discontinued":
                    connection.execute("UPDATE cycle_medications SET status = 'held', actual_dose = NULL WHERE cycle_id = ?", (cycle_id,))
                    connection.execute(
                        """UPDATE treatment_cycles SET status = 'discontinued', modification_reason = ?, reason_category = ?,
                               recorded_by = ?, recorded_at = ?, updated_at = ? WHERE id = ?""",
                        (rationale, category, recorded_by, now, now, cycle_id))
                    connection.execute("""UPDATE treatment_cycles SET status = 'cancelled', updated_at = ? WHERE treatment_plan_id = ?
                                          AND cycle_number > ? AND status IN ('planned','delayed')""", (now, cycle["treatment_plan_id"], cycle["cycle_number"]))
                    connection.execute("UPDATE treatment_plans SET status = 'discontinued', updated_at = ? WHERE id = ?", (now, plan["id"]))
                sync_cycle_summary(connection, cycle_id)
                analysis = self._analysis(connection, plan["id"])
                connection.execute(
                    """INSERT INTO cycle_decisions (cycle_id, outcome, reason_category, rationale, evidence_json, decided_by, decided_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (cycle_id, outcome, category, rationale, decision_evidence(analysis, cycle_id), recorded_by, now))
                updated = self._cycle_with_meds(connection, cycle_id)
                updated["decisions"] = self._decisions(connection, cycle_id)
                next_cycle = row_or_none(connection, """SELECT cycle_number, planned_date FROM treatment_cycles WHERE treatment_plan_id = ?
                                                        AND status IN ('planned', 'delayed') ORDER BY planned_date LIMIT 1""", (cycle["treatment_plan_id"],))
            if payload.get("notify_whatsapp"):
                # The doctor's decision reaches the patient: a new date updates the plan; after a given cycle, a reminder for the next one.
                sent = []
                if outcome in ("delayed", "held", "discontinued"):
                    sent.append(whatsapp.notify(database_path, plan["patient_id"], "plan", self._clinician_name()))
                if outcome in ("given", "modified", "delayed") and next_cycle and next_cycle["planned_date"] > date.today().isoformat():
                    sent.append(whatsapp.notify(database_path, plan["patient_id"], "reminder", self._clinician_name(), visit_date=next_cycle["planned_date"],
                                                visit_time="09:00", purpose=f"Chemotherapy cycle {next_cycle['cycle_number']}"))
                updated["whatsapp"] = sent
            return {"data": updated, "meta": {"previous_version_preserved": True}}

        def _decisions(self, connection: sqlite3.Connection, cycle_id: int) -> list[dict[str, Any]]:
            data = rows(connection, "SELECT * FROM cycle_decisions WHERE cycle_id = ? ORDER BY decided_at DESC, id DESC", (cycle_id,))
            for item in data:
                raw = item.pop("evidence_json")
                item["evidence"] = json.loads(raw) if raw else None
            return data

        def _cycle_decisions(self, cycle_id: int) -> dict[str, Any]:
            self._ensure("treatment_cycles", cycle_id)
            with transaction(database_path) as connection:
                data = self._decisions(connection, cycle_id)
            return {"data": data, "count": len(data)}

        def _cycle_revisions(self, cycle_id: int) -> dict[str, Any]:
            self._ensure("treatment_cycles", cycle_id)
            with transaction(database_path) as connection:
                data = rows(connection, "SELECT id, cycle_id, revision_number, snapshot_json, changed_at FROM treatment_cycle_revisions WHERE cycle_id = ? ORDER BY revision_number DESC", (cycle_id,))
            for item in data:
                item["snapshot"] = json.loads(item.pop("snapshot_json"))
            return {"data": data, "count": len(data)}

        # ------------------------------------------------------------------ longitudinal analysis
        def _analysis(self, connection: sqlite3.Connection, plan_id: int) -> dict[str, Any]:
            plan = row_or_none(connection, "SELECT * FROM treatment_plans WHERE id = ?", (plan_id,))
            if not plan:
                raise ApiError(404, "Treatment plan not found")
            cycles = rows(connection, "SELECT * FROM treatment_cycles WHERE treatment_plan_id = ? ORDER BY cycle_number", (plan_id,))
            ids = [c["id"] for c in cycles]
            marks = ",".join("?" * len(ids)) or "NULL"
            meds: dict[int, list[dict[str, Any]]] = {}
            for med in rows(connection, f"SELECT * FROM cycle_medications WHERE cycle_id IN ({marks}) ORDER BY sort_order, id", tuple(ids)):
                meds.setdefault(med["cycle_id"], []).append(med)
            labs = rows(connection, f"SELECT * FROM lab_investigations WHERE cycle_id IN ({marks})", tuple(ids))
            first_date = cycles[0]["planned_date"] if cycles else "0000-01-01"
            symptoms = rows(connection, f"""SELECT * FROM symptoms WHERE patient_id = ? AND (cycle_id IN ({marks})
                                OR (cycle_id IS NULL AND onset_date >= ?))""", (plan["patient_id"], *ids, first_date))
            decisions: dict[int, list[dict[str, Any]]] = {}
            for cycle_id in ids:
                decisions[cycle_id] = self._decisions(connection, cycle_id)
            documents = rows(connection, f"SELECT * FROM lab_documents WHERE cycle_id IN ({marks}) ORDER BY uploaded_at", tuple(ids))
            plan_meds = rows(connection, "SELECT * FROM plan_medications WHERE treatment_plan_id = ? ORDER BY sort_order, id", (plan_id,))
            patient = row_or_none(connection, "SELECT * FROM patients WHERE id = ?", (plan["patient_id"],))
            drug_names = sorted({m["drug_name"] for m in plan_meds} | {m["drug_name"] for ms in meds.values() for m in ms})
            prefetch_async(drug_names)
            labels = dailymed_for_drugs(drug_names)
            result = analyse_plan(plan, cycles, meds, labs, symptoms, decisions, documents, plan_meds, labels)
            reports = [r for r in portal.list_reports(connection, plan["patient_id"]) if r["cycle_id"] in ids or r["cycle_id"] is None]
            result["patient_reports"] = reports
            for cycle in result["cycles"]:
                open_reports = [r for r in reports if r["cycle_id"] == cycle["id"] and r["status"] == "new"]
                if open_reports:
                    urgent = any(r["red_flags"] for r in open_reports)
                    cycle["attention"].insert(0, {"level": "alert" if urgent else "warning", "kind": "patient_report",
                                                  "text": f"{len(open_reports)} patient-reported symptom message(s) to review" + (" — red flags" if urgent else ""),
                                                  "detail": "; ".join(r["text"][:90] for r in open_reports[:2])})
                cycle["patient_reports"] = [r["id"] for r in reports if r["cycle_id"] == cycle["id"]]
            result["context"] = {"dailymed": {d: {"title": c.get("title"), "url": c.get("url"), "published_date": c.get("published_date"),
                                                  "fetched_at": c.get("fetched_at"), "terms": sorted(c.get("terms", {}))} for d, c in labels.items()},
                                 "dailymed_missing": [d for d in drug_names if d not in labels],
                                 "ctcae": {"version": CTCAE_VERSION_NOW(), "provenance_note": "Per-term criteria source is shown on each item."}}
            result["patient"] = patient
            return result

        def _longitudinal(self, plan_id: int) -> dict[str, Any]:
            with transaction(database_path) as connection:
                return {"data": self._analysis(connection, plan_id)}

        # ------------------------------------------------------------------ labs
        def _list_labs(self, cycle_id: int) -> dict[str, Any]:
            self._ensure("treatment_cycles", cycle_id)
            with transaction(database_path) as connection:
                data = rows(connection, "SELECT * FROM lab_investigations WHERE cycle_id = ? ORDER BY collection_date, test_name", (cycle_id,))
            return {"data": data, "count": len(data)}

        def _lab_values(self, payload: dict[str, Any]) -> dict[str, Any]:
            require(payload, ["test_name", "value", "unit", "collection_date"])
            validate_dates(payload, ["collection_date"])
            if payload["collection_date"] > date.today().isoformat():
                raise ApiError(422, "Collection date cannot be in the future")
            try:
                value = float(payload["value"])
            except (TypeError, ValueError):
                raise ApiError(422, f"{payload['test_name']} value must be a number") from None
            if not math.isfinite(value):
                raise ApiError(422, "Lab value must be finite")
            refs = {}
            for key in ("ref_low", "ref_high"):
                refs[key] = None if payload.get(key) in (None, "") else float(payload[key])
            if refs["ref_low"] is not None and refs["ref_high"] is not None and refs["ref_low"] > refs["ref_high"]:
                raise ApiError(422, f"{payload['test_name']}: reference low is above reference high")
            status = payload.get("verification_status", "verified")
            if status not in ("verified", "unverified", "rejected"):
                raise ApiError(422, "Invalid verification status")
            return {"test_name": clean_text(payload["test_name"]), "test_code": clean_text(payload.get("test_code")), "value": value,
                    "unit": clean_text(payload["unit"]), **refs, "collection_date": payload["collection_date"],
                    "verification_status": status, "source": payload.get("source", "manual"),
                    "source_document": payload.get("source_document"), "document_id": payload.get("document_id"),
                    "verified_by": clean_text(payload.get("verified_by")) or (self._clinician_name() if status == "verified" else None)}

        def _insert_lab(self, connection: sqlite3.Connection, patient_id: int, cycle_id: int, lab: dict[str, Any]) -> int:
            now = utc_now()
            return connection.execute(
                """INSERT INTO lab_investigations (patient_id, cycle_id, test_name, test_code, value, unit, ref_low, ref_high,
                       collection_date, verification_status, source, source_document, verified_by, document_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (patient_id, cycle_id, lab["test_name"], lab["test_code"], lab["value"], lab["unit"], lab["ref_low"], lab["ref_high"],
                 lab["collection_date"], lab["verification_status"], lab["source"], lab["source_document"], lab["verified_by"],
                 lab["document_id"], now, now),
            ).lastrowid

        def _cycle_patient(self, connection: sqlite3.Connection, cycle_id: int) -> dict[str, Any]:
            cycle = row_or_none(connection, """SELECT c.*, p.patient_id FROM treatment_cycles c JOIN treatment_plans p ON p.id = c.treatment_plan_id WHERE c.id = ?""", (cycle_id,))
            if not cycle:
                raise ApiError(404, "Treatment cycle not found")
            return cycle

        def _create_labs(self, cycle_id: int) -> dict[str, Any]:
            payload = self._body()
            batch = payload.get("results")
            if batch is not None:
                if not isinstance(batch, list) or not batch or len(batch) > 60:
                    raise ApiError(422, "results must be a list of 1–60 lab values")
                shared = {k: payload.get(k) for k in ("collection_date", "verified_by", "source") if payload.get(k)}
                values = [self._lab_values({**shared, **item}) for item in batch]
            else:
                values = [self._lab_values(payload)]
            with transaction(database_path) as connection:
                cycle = self._cycle_patient(connection, cycle_id)
                ids = [self._insert_lab(connection, cycle["patient_id"], cycle_id, lab) for lab in values]
                data = [row_or_none(connection, "SELECT * FROM lab_investigations WHERE id = ?", (i,)) for i in ids]
            return {"data": data[0] if batch is None else data, "count": len(data)}

        def _update_lab(self, lab_id: int) -> dict[str, Any]:
            payload = self._body()
            validate_dates(payload, ["collection_date"])
            if "value" in payload:
                try:
                    payload["value"] = float(payload["value"])
                except (TypeError, ValueError):
                    raise ApiError(422, "value must be a number") from None
            if "verification_status" in payload and payload["verification_status"] not in ("verified", "unverified", "rejected"):
                raise ApiError(422, "Invalid verification status")
            return self._update_record("lab_investigations", lab_id, payload, ["test_name", "test_code", "value", "unit", "ref_low", "ref_high", "collection_date", "verification_status", "source", "source_document", "verified_by"])

        def _verify_lab(self, lab_id: int) -> dict[str, Any]:
            payload = self._body()
            with transaction(database_path) as connection:
                lab = row_or_none(connection, "SELECT * FROM lab_investigations WHERE id = ?", (lab_id,))
                if not lab:
                    raise ApiError(404, "Lab value not found")
            merged = {**lab, **{k: v for k, v in payload.items() if k in ("test_name", "value", "unit", "ref_low", "ref_high", "collection_date")}}
            merged["verification_status"] = "verified"
            merged["verified_by"] = clean_text(payload.get("verified_by")) or self._clinician_name()
            values = self._lab_values(merged)
            with transaction(database_path) as connection:
                connection.execute(
                    """UPDATE lab_investigations SET test_name = ?, value = ?, unit = ?, ref_low = ?, ref_high = ?, collection_date = ?,
                           verification_status = 'verified', verified_by = ?, updated_at = ? WHERE id = ?""",
                    (values["test_name"], values["value"], values["unit"], values["ref_low"], values["ref_high"], values["collection_date"],
                     values["verified_by"], utc_now(), lab_id))
                data = row_or_none(connection, "SELECT * FROM lab_investigations WHERE id = ?", (lab_id,))
            return {"data": data}

        def _list_documents(self, cycle_id: int) -> dict[str, Any]:
            self._ensure("treatment_cycles", cycle_id)
            with transaction(database_path) as connection:
                data = rows(connection, "SELECT * FROM lab_documents WHERE cycle_id = ? ORDER BY uploaded_at DESC", (cycle_id,))
            return {"data": data, "count": len(data)}

        def _upload_document(self, cycle_id: int) -> dict[str, Any]:
            payload = self._body(MAX_UPLOAD_JSON)
            require(payload, ["filename", "data_base64"])
            validate_dates(payload, ["collection_date", "recording_date"])
            recording_date = payload.get("recording_date") or payload.get("collection_date")
            date_confirmed = payload.get("recording_date_confirmed", True) is not False
            if recording_date and recording_date > date.today().isoformat():
                raise ApiError(422, "Date of recording cannot be in the future", {"fields": ["recording_date"]})
            filename = Path(str(payload["filename"])).name[:120] or "report"
            content_type = str(payload.get("content_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream")
            allowed = ("application/pdf", "image/png", "image/jpeg", "image/webp", "text/plain", "text/csv")
            if content_type not in allowed and not filename.lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".txt", ".csv")):
                raise ApiError(415, "Upload a PDF, image, text or CSV lab report")
            try:
                data = base64.b64decode(str(payload["data_base64"]), validate=True)
            except (binascii.Error, ValueError):
                raise ApiError(422, "File content is not valid base64") from None
            if not data or len(data) > 10_000_000:
                raise ApiError(413, "File must be between 1 byte and 10 MB")
            with transaction(database_path) as connection:
                cycle = self._cycle_patient(connection, cycle_id)
            extraction = extract_candidates(data, content_type, filename)
            report_date = extraction.get("report_date")
            if not date_confirmed and report_date and report_date <= date.today().isoformat():
                recording_date = report_date  # the prefilled default was not changed: trust the date printed on the report
            collection_date = recording_date or report_date or min(cycle["planned_date"], date.today().isoformat())
            if collection_date > date.today().isoformat():
                collection_date = date.today().isoformat()
            date_note = None
            cycle_day = cycle.get("administered_date") or cycle["planned_date"]
            if cycle["status"] in ("given", "modified") and collection_date > cycle_day:
                date_note = f"Collected {collection_date}, after cycle {cycle['cycle_number']} was given ({cycle_day}) — check that the right cycle window was selected."
            if recording_date and report_date and report_date != recording_date:
                date_note = (date_note + " " if date_note else "") + f"Report shows collection date {report_date}; values saved against the selected recording date {recording_date}."
            upload_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
            stored = upload_dir / f"{uuid.uuid4().hex}-{safe}"
            stored.write_bytes(data)
            method_label = f"AI extraction ({extraction['model']})" if extraction["method"] == "claude" else "text-layer parser"
            note_parts = [p for p in (date_note, extraction["note"], f"{method_label}; source: {extraction['text_source']}.") if p]
            with transaction(database_path) as connection:
                document_id = connection.execute(
                    """INSERT INTO lab_documents (patient_id, cycle_id, filename, content_type, stored_path, size_bytes,
                           extraction_method, extraction_note, recording_date, report_date, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (cycle["patient_id"], cycle_id, filename, content_type, str(stored), len(data), extraction["method"], " ".join(note_parts),
                     collection_date, report_date, utc_now()),
                ).lastrowid
                ids = []
                for candidate in extraction["candidates"]:
                    catalog = LAB_CATALOG.get(candidate["test_name"], {})
                    lab = {"test_name": candidate["test_name"], "test_code": None, "value": candidate["value"], "unit": candidate["unit"],
                           "ref_low": candidate["ref_low"] if candidate.get("ref_low") is not None else catalog.get("low"),
                           "ref_high": candidate["ref_high"] if candidate.get("ref_high") is not None else catalog.get("high"),
                           "collection_date": collection_date, "verification_status": "unverified",
                           "source": f"upload:{extraction['method']}", "source_document": (candidate.get("note") or candidate.get("raw_line") or "")[:200],
                           "verified_by": None, "document_id": document_id}
                    ids.append(self._insert_lab(connection, cycle["patient_id"], cycle_id, lab))
                document = row_or_none(connection, "SELECT * FROM lab_documents WHERE id = ?", (document_id,))
                candidates = [row_or_none(connection, "SELECT * FROM lab_investigations WHERE id = ?", (i,)) for i in ids]
            document.pop("stored_path", None)
            return {"data": {"document": document, "candidates": candidates},
                    "meta": {"verified": False, "method": extraction["method"], "model": extraction.get("model"), "report_date": report_date,
                             "recording_date": collection_date, "ai_enabled": ai_status()["enabled"], "note": extraction.get("note")}}

        def _serve_document(self, document_id: int) -> None:
            with transaction(database_path) as connection:
                document = row_or_none(connection, "SELECT * FROM lab_documents WHERE id = ?", (document_id,))
            if not document:
                raise ApiError(404, "Document not found")
            path = Path(document["stored_path"]).resolve()
            if upload_dir.resolve() not in path.parents or not path.is_file():
                raise ApiError(404, "Document file is missing")
            content = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", document["content_type"] or "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", f'inline; filename="{document["filename"]}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)

        def _lab_trends(self, patient_id: int, query: dict[str, list[str]]) -> dict[str, Any]:
            self._ensure("patients", patient_id)
            params: list[Any] = [patient_id]
            where = "patient_id = ? AND verification_status = 'verified'"
            if query.get("test_name"):
                where += " AND test_name = ?"
                params.append(query["test_name"][0])
            with transaction(database_path) as connection:
                data = rows(connection, f"SELECT * FROM lab_investigations WHERE {where} ORDER BY test_name, collection_date, id", tuple(params))
            grouped: dict[str, list[dict[str, Any]]] = {}
            for item in data:
                grouped.setdefault(item["test_name"], []).append(item)
            trends = [trend_payload(group) for group in grouped.values()]
            trends.sort(key=lambda item: item["test_name"])
            return {"data": trends, "count": len(trends), "meta": {"verified_only": True, "clinical_recommendation": False}}

        # ------------------------------------------------------------------ symptoms / CTCAE
        def _suggest_terms(self, query: dict[str, list[str]]) -> dict[str, Any]:
            text = query.get("text", [""])[0].strip().lower()
            drugs = [d for d in query.get("drugs", [""])[0].split(",") if d.strip()]
            shortlist = []
            for drug in drugs:
                canonical = normalise_drug(drug)
                if canonical:
                    from .reference import DRUG_TOXICITIES
                    for term in DRUG_TOXICITIES[canonical]["terms"]:
                        if CTCAE_TERMS[term]["kind"] == "clinical" and term not in shortlist:
                            shortlist.append(term)
            matches = []
            if text:
                for term, info in CTCAE_TERMS.items():
                    if info["kind"] != "clinical":
                        continue
                    hits = [k for k in SYMPTOM_KEYWORDS.get(term, []) if k in text]
                    if term.lower() in text or any(word in term.lower() for word in text.split() if len(word) > 3):
                        hits.append(term.lower())
                    if hits:
                        matches.append({"term": term, "matched": sorted(set(hits)), "associated_drugs": drugs_associated_with(term, drugs)})
                matches.sort(key=lambda m: (-len(m["associated_drugs"]), -len(m["matched"]), m["term"]))
            return {"data": {"matches": matches, "drug_shortlist": shortlist},
                    "meta": {"note": "Keyword search aid only. The clinician selects the term and confirms the grade."}}

        def _symptom_values(self, payload: dict[str, Any], partial: bool = False) -> None:
            validate_dates(payload, ["onset_date", "resolution_date"])
            require_date_order(payload, "onset_date", "resolution_date")
            if "ctcae_term" in payload and payload["ctcae_term"] not in CTCAE_TERMS:
                raise ApiError(422, "Unknown CTCAE term; choose one from the stored reference list")
            if "grade" in payload:
                try:
                    grade = int(payload["grade"])
                except (TypeError, ValueError):
                    raise ApiError(422, "CTCAE grade must be between 1 and 5") from None
                if not 1 <= grade <= 5:
                    raise ApiError(422, "CTCAE grade must be between 1 and 5")
                term = payload.get("ctcae_term")
                if term and grade not in CTCAE_TERMS[term]["grades"]:
                    raise ApiError(422, f"Grade {grade} is not defined for {term}")
                payload["grade"] = grade
            if "status" in payload and payload["status"] not in ("ongoing", "improving", "resolved"):
                raise ApiError(422, "status must be ongoing, improving or resolved")
            if payload.get("onset_date") and payload["onset_date"] > date.today().isoformat():
                raise ApiError(422, "Onset date cannot be in the future")

        def _list_symptoms(self, patient_id: int, query: dict[str, list[str]]) -> dict[str, Any]:
            self._ensure("patients", patient_id)
            params: list[Any] = [patient_id]
            where = "patient_id = ?"
            for key in ("cycle_id", "status"):
                if query.get(key):
                    where += f" AND {key} = ?"
                    params.append(query[key][0])
            with transaction(database_path) as connection:
                data = rows(connection, f"SELECT * FROM symptoms WHERE {where} ORDER BY onset_date DESC, id DESC", tuple(params))
            return {"data": data, "count": len(data)}

        def _create_symptom(self, patient_id: int) -> dict[str, Any]:
            self._ensure("patients", patient_id)
            payload = self._body()
            require(payload, ["ctcae_term", "grade", "onset_date", "confirmed_by"])
            self._symptom_values(payload)
            now = utc_now()
            with transaction(database_path) as connection:
                if payload.get("cycle_id"):
                    cycle = row_or_none(connection, """SELECT p.patient_id FROM treatment_cycles c JOIN treatment_plans p ON p.id = c.treatment_plan_id WHERE c.id = ?""", (payload["cycle_id"],))
                    if not cycle or cycle["patient_id"] != patient_id:
                        raise ApiError(422, "cycle_id does not belong to this patient")
                symptom_id = connection.execute(
                    """INSERT INTO symptoms (patient_id, cycle_id, ctcae_term, ctcae_version, grade, onset_date, resolution_date,
                           status, note, suspected_drug, confirmed_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (patient_id, payload.get("cycle_id"), payload["ctcae_term"], payload.get("ctcae_version", "6.0"), payload["grade"],
                     payload["onset_date"], payload.get("resolution_date") or None, payload.get("status", "ongoing"), clean_text(payload.get("note")),
                     clean_text(payload.get("suspected_drug")), payload["confirmed_by"], now, now),
                ).lastrowid
                symptom = row_or_none(connection, "SELECT * FROM symptoms WHERE id = ?", (symptom_id,))
            return {"data": symptom}

        def _update_symptom(self, symptom_id: int) -> dict[str, Any]:
            payload = self._body()
            with transaction(database_path) as connection:
                symptom = row_or_none(connection, "SELECT * FROM symptoms WHERE id = ?", (symptom_id,))
                if not symptom:
                    raise ApiError(404, "Record not found")
            merged_check = {**symptom, **payload}
            self._symptom_values({k: merged_check[k] for k in ("ctcae_term", "grade", "onset_date", "resolution_date", "status") if merged_check.get(k) is not None})
            if "grade" in payload:
                payload["grade"] = int(payload["grade"])
            if "cycle_id" in payload and payload["cycle_id"] is not None:
                with transaction(database_path) as connection:
                    cycle = row_or_none(connection, """SELECT p.patient_id FROM treatment_cycles c JOIN treatment_plans p ON p.id = c.treatment_plan_id WHERE c.id = ?""", (payload["cycle_id"],))
                    if not cycle or cycle["patient_id"] != symptom["patient_id"]:
                        raise ApiError(422, "cycle_id does not belong to this patient")
            if "resolution_date" in payload and not payload["resolution_date"]:
                payload["resolution_date"] = None
            return self._update_record("symptoms", symptom_id, payload, ["cycle_id", "ctcae_term", "ctcae_version", "grade", "onset_date", "resolution_date", "status", "note", "suspected_drug", "confirmed_by"])

        # ------------------------------------------------------------------ patient portal (doctor side)
        def _portal_status(self, patient_id: int) -> dict[str, Any]:
            self._ensure("patients", patient_id)
            with transaction(database_path) as connection:
                return {"data": portal.access_status(connection, patient_id)}

        def _portal_enable(self, patient_id: int) -> dict[str, Any]:
            self._ensure("patients", patient_id)
            with transaction(database_path) as connection:
                portal.set_access(connection, patient_id, True)
                return {"data": portal.access_status(connection, patient_id)}

        def _portal_disable(self, patient_id: int) -> dict[str, Any]:
            self._ensure("patients", patient_id)
            with transaction(database_path) as connection:
                portal.set_access(connection, patient_id, False)
                return {"data": portal.access_status(connection, patient_id)}

        def _patient_reports(self, patient_id: int | None, query: dict[str, list[str]]) -> dict[str, Any]:
            if patient_id is not None:
                self._ensure("patients", patient_id)
            with transaction(database_path) as connection:
                data = portal.list_reports(connection, patient_id, query.get("status", [None])[0])
            return {"data": data, "count": len(data), "open": sum(1 for r in data if r["status"] == "new")}

        def _patient_report(self, report_id: int) -> dict[str, Any]:
            with transaction(database_path) as connection:
                row = row_or_none(connection, "SELECT * FROM patient_reports WHERE id = ?", (report_id,))
                if not row:
                    raise ApiError(404, "Report not found")
                return {"data": portal.report_view(row)}

        def _review_patient_report(self, report_id: int) -> dict[str, Any]:
            payload = self._body()
            payload.setdefault("reviewed_by", self._clinician_name())
            with transaction(database_path) as connection:
                before = row_or_none(connection, "SELECT patient_message FROM patient_reports WHERE id = ?", (report_id,))
                result = portal.review_report(connection, report_id, payload)
            if before is not None and result.get("patient_message") and result["patient_message"] != before["patient_message"]:
                result["whatsapp"] = whatsapp.send_report_reply(database_path, report_id, self._clinician_name(), upload_dir)
            return {"data": result}

        # ------------------------------------------------------------------ patient portal (patient side)
        def _cookie(self, name: str) -> str | None:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception:
                return None
            morsel = cookie.get(name)
            return morsel.value if morsel else None

        def _cookie_token(self) -> str | None:
            return self._cookie(PORTAL_COOKIE)

        def _clinician_name(self) -> str:
            return (self.clinician or {}).get("name") or CLINICIAN

        def _auth_route(self, method: str, path: str) -> tuple[dict[str, Any], int]:
            if path == "/api/auth/login" and method == "POST":
                body = self._body()
                with transaction(database_path) as connection:
                    token, clinician = auth.login_clinician(connection, str(body.get("username") or ""), str(body.get("password") or ""))
                self._extra_headers.append(("Set-Cookie", f"{auth.DOCTOR_COOKIE}={token}; {cookie_attrs()}; Path=/; Max-Age={auth.SESSION_HOURS * 3600}"))
                return {"data": clinician}, HTTPStatus.OK
            if path == "/api/auth/logout" and method == "POST":
                with transaction(database_path) as connection:
                    auth.logout_clinician(connection, self._cookie(auth.DOCTOR_COOKIE))
                self._extra_headers.append(("Set-Cookie", f"{auth.DOCTOR_COOKIE}=; {cookie_attrs()}; Path=/; Max-Age=0"))
                return {"data": {"signed_out": True}}, HTTPStatus.OK
            if path == "/api/auth/me" and method == "GET":
                with transaction(database_path) as connection:
                    clinician = auth.session_clinician(connection, self._cookie(auth.DOCTOR_COOKIE))
                if not clinician:
                    raise auth.AuthError(401, "Please sign in.")
                return {"data": clinician}, HTTPStatus.OK
            raise ApiError(404, "Endpoint not found")

        def _portal_patient(self) -> int:
            with transaction(database_path) as connection:
                patient_id = portal.session_patient(connection, self._cookie_token())
            if not patient_id:
                raise PortalError(401, "Please sign in again.")
            return patient_id

        def _portal_route(self, method: str, path: str) -> tuple[dict[str, Any], int]:
            if path == "/api/portal/login" and method == "POST":
                body = self._body()
                with transaction(database_path) as connection:
                    token, me = portal.login(connection, str(body.get("phone") or ""), str(body.get("password") or ""))
                self._extra_headers.append(("Set-Cookie", f"{PORTAL_COOKIE}={token}; {cookie_attrs()}; Path=/; Max-Age={portal.SESSION_HOURS * 3600}"))
                return {"data": me}, HTTPStatus.OK
            if path == "/api/portal/logout" and method == "POST":
                with transaction(database_path) as connection:
                    portal.logout(connection, self._cookie_token())
                self._extra_headers.append(("Set-Cookie", f"{PORTAL_COOKIE}=; {cookie_attrs()}; Path=/; Max-Age=0"))
                return {"data": {"signed_out": True}}, HTTPStatus.OK
            patient_id = self._portal_patient()
            if path == "/api/portal/me" and method == "GET":
                with transaction(database_path) as connection:
                    return {"data": portal.portal_me(connection, patient_id)}, HTTPStatus.OK
            if path == "/api/portal/plans" and method == "GET":
                with transaction(database_path) as connection:
                    return {"data": portal.portal_plans(connection, patient_id)}, HTTPStatus.OK
            if path == "/api/portal/radiotherapy" and method == "GET":
                with transaction(database_path) as connection:
                    return {"data": portal.portal_radiotherapy(connection, patient_id)}, HTTPStatus.OK
            if path == "/api/portal/surgery" and method == "GET":
                with transaction(database_path) as connection:
                    return {"data": portal.portal_surgery(connection, patient_id)}, HTTPStatus.OK
            if path == "/api/portal/summaries" and method == "GET":
                from . import discharge
                return {"data": discharge.for_patient(database_path, patient_id)}, HTTPStatus.OK
            if path == "/api/portal/labs" and method == "GET":
                with transaction(database_path) as connection:
                    return {"data": portal.portal_labs(connection, patient_id)}, HTTPStatus.OK
            if path == "/api/portal/reports" and method == "GET":
                with transaction(database_path) as connection:
                    return {"data": portal.list_reports(connection, patient_id, for_patient=True)}, HTTPStatus.OK
            if path == "/api/portal/checkin/bank" and method == "GET":
                with transaction(database_path) as connection:
                    return {"data": portal.checkin_bank(connection, patient_id)}, HTTPStatus.OK
            if path == "/api/portal/checkin/detect" and method == "POST":
                body = self._body()
                return {"data": portal.detect_symptoms(str(body.get("text") or ""))}, HTTPStatus.OK
            if path == "/api/portal/checkin/interpret" and method == "POST":
                body = self._body()
                try:
                    result = voice_answers.interpret(str(body.get("symptom") or ""), str(body.get("question") or ""),
                                                     str(body.get("heard") or ""), str(body.get("lang") or "en"))
                except questionnaire.CheckinError as error:
                    raise PortalError(422, str(error)) from None
                return {"data": result}, HTTPStatus.OK
            if path == "/api/portal/reports" and method == "POST":
                body = self._body(MAX_UPLOAD_JSON)
                body.pop("_sync_triage", None)
                if getattr(self.server, "sync_triage", False):
                    body["_sync_triage"] = True
                return {"data": portal.create_report(database_path, patient_id, body, upload_dir)}, HTTPStatus.CREATED
            raise PortalError(404, "Not found")

        def _binary_route(self, method: str, path: str) -> tuple[Path, str, str | None] | None:
            if method != "GET":
                return None
            match = re.fullmatch(r"/api/patient-reports/(\d+)/audio", path)
            if match:
                with transaction(database_path) as connection:
                    file_path, content_type = portal.audio_for(connection, int(match.group(1)))
                return file_path, content_type, None
            match = re.fullmatch(r"/api/portal/reports/(\d+)/audio", path)
            if match:
                patient_id = self._portal_patient()
                with transaction(database_path) as connection:
                    file_path, content_type = portal.audio_for(connection, int(match.group(1)), patient_id)
                return file_path, content_type, None
            match = re.fullmatch(r"/api/portal/summaries/(\d+)/print", path)
            if match:
                from . import discharge
                patient_id = self._portal_patient()
                html_page = discharge.print_html(database_path, int(match.group(1)), patient_id=patient_id)
                return routes_ext.RawResponse(html_page.encode(), "text/html; charset=utf-8")
            match = re.fullmatch(r"/api/portal/documents/(\d+)/file", path)
            if match:
                patient_id = self._portal_patient()
                with transaction(database_path) as connection:
                    doc = portal.patient_document(connection, patient_id, int(match.group(1)))
                return Path(doc["stored_path"]), doc["content_type"] or "application/octet-stream", doc["filename"]
            return None

        def _send_raw(self, raw: "routes_ext.RawResponse") -> None:
            if raw.content_type.startswith("file:"):
                return self._send_file(Path(raw.content.decode()), raw.content_type[5:], raw.filename)
            self.send_response(200)
            self.send_header("Content-Type", raw.content_type)
            self.send_header("Content-Length", str(len(raw.content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if raw.filename:
                self.send_header("Content-Disposition", f'inline; filename="{raw.filename}"')
            self.end_headers()
            self.wfile.write(raw.content)

        def _whatsapp_webhook(self, method: str, parsed: Any) -> None:
            """Meta WhatsApp Cloud API webhook: GET verification handshake and POST message/status events."""
            if method == "GET":
                status, text = whatsapp.verify_webhook(parse_qs(parsed.query))
                body = text.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if method != "POST":
                return self._json(405, {"error": {"message": "Method not allowed"}})
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_UPLOAD_JSON:
                return self._json(413, {"error": {"message": "Payload too large"}})
            raw = self.rfile.read(length) if length else b""
            status, result = whatsapp.handle_webhook(database_path, raw, self.headers.get("X-Hub-Signature-256"), upload_dir)
            return self._json(status, result)

        def _send_file(self, file_path: Path, content_type: str, filename: str | None) -> None:
            resolved = file_path.resolve()
            if upload_dir.resolve() not in resolved.parents or not resolved.is_file():
                raise PortalError(404, "File is missing")
            content = resolved.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if filename:
                self.send_header("Content-Disposition", f'inline; filename="{filename}"')
            self.end_headers()
            self.wfile.write(content)

        # ------------------------------------------------------------------ generic helpers
        def _get_record(self, table: str, record_id: int) -> dict[str, Any]:
            with transaction(database_path) as connection:
                record = row_or_none(connection, f"SELECT * FROM {table} WHERE id = ?", (record_id,))
            if not record:
                raise ApiError(404, "Record not found")
            return {"data": record}

        def _update_record(self, table: str, record_id: int, payload: dict[str, Any], allowed: list[str]) -> dict[str, Any]:
            with transaction(database_path) as connection:
                if not row_or_none(connection, f"SELECT id FROM {table} WHERE id = ?", (record_id,)):
                    raise ApiError(404, "Record not found")
                fields = [field for field in allowed if field in payload]
                if fields:
                    self._apply_update(connection, table, record_id, payload, fields)
                elif not (table == "treatment_plans" and "medications" in payload):
                    raise ApiError(422, "No supported fields supplied")
                record = row_or_none(connection, f"SELECT * FROM {table} WHERE id = ?", (record_id,))
            return {"data": record}

        def _apply_update(self, connection: sqlite3.Connection, table: str, record_id: int, payload: dict[str, Any], allowed: list[str]) -> None:
            fields = [field for field in allowed if field in payload]
            if not fields:
                raise ApiError(422, "No supported fields supplied")
            assignments = ", ".join(f"{field} = ?" for field in fields) + ", updated_at = ?"
            connection.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", tuple(payload[field] for field in fields) + (utc_now(), record_id))

        def _delete(self, table: str, record_id: int) -> dict[str, Any]:
            with transaction(database_path) as connection:
                documents = []
                if table == "patients":
                    documents = rows(connection, "SELECT stored_path FROM lab_documents WHERE patient_id = ?", (record_id,))
                cursor = connection.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,))
                if cursor.rowcount == 0:
                    raise ApiError(404, "Record not found")
            for document in documents:
                try:
                    Path(document["stored_path"]).unlink(missing_ok=True)
                except OSError:
                    pass
            return {"data": {"id": record_id, "deleted": True}}

        def _ensure(self, table: str, record_id: int) -> None:
            with transaction(database_path) as connection:
                if not row_or_none(connection, f"SELECT id FROM {table} WHERE id = ?", (record_id,)):
                    raise ApiError(404, "Record not found")

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in getattr(self, "_extra_headers", []):
                self.send_header(name, value)
            self._cors_headers()
            self.end_headers()
            self.wfile.write(body)

        def _cors_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:4173")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _serve_static(self, request_path: str) -> None:
            if not web_root:
                return self._json(404, {"error": {"message": "Not found"}})
            pages = {"": "home.html", "/": "home.html", "/clinician": "index.html", "/clinician/": "index.html", "/portal": "portal.html", "/portal/": "portal.html"}
            relative = pages.get(request_path, request_path.lstrip("/"))
            if relative not in {"home.html", "home.css", "index.html", "app.js", "styles.css", "favicon.ico", "portal.html", "portal.js", "portal.css",
                                "rt.js", "surgery.js", "records.js", "whatsapp.js"}:
                return self._json(404, {"error": {"message": "Not found"}})
            target = (web_root / relative).resolve()
            if web_root not in target.parents and target != web_root:
                return self._json(403, {"error": {"message": "Forbidden"}})
            if not target.is_file():
                return self._json(404, {"error": {"message": "Not found"}})
            content = target.read_bytes()
            # The home page carries its own beta notice, so the demo banner is only added to the apps.
            banner = demo_banner_html() if target.suffix == ".html" and relative != "home.html" else b""
            if banner:
                match = re.search(rb"<body[^>]*>", content)
                if match:
                    content = content[:match.end()] + banner + content[match.end():]
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)

    return Handler

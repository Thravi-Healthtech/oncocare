from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    medical_record_number TEXT NOT NULL UNIQUE,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    date_of_birth TEXT,
    sex TEXT,
    diagnosis TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS treatment_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    regimen_name TEXT NOT NULL,
    intent TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    start_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_medications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    treatment_plan_id INTEGER NOT NULL REFERENCES treatment_plans(id) ON DELETE CASCADE,
    drug_name TEXT NOT NULL,
    dose REAL NOT NULL CHECK (dose >= 0),
    dose_unit TEXT NOT NULL,
    route TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS treatment_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    treatment_plan_id INTEGER NOT NULL REFERENCES treatment_plans(id) ON DELETE CASCADE,
    cycle_number INTEGER NOT NULL CHECK (cycle_number > 0),
    planned_date TEXT NOT NULL,
    administered_date TEXT,
    status TEXT NOT NULL DEFAULT 'planned',
    drug_name TEXT NOT NULL,
    planned_dose REAL NOT NULL CHECK (planned_dose >= 0),
    actual_dose REAL CHECK (actual_dose >= 0),
    dose_unit TEXT NOT NULL DEFAULT 'mg/m2',
    route TEXT,
    modification_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (treatment_plan_id, cycle_number)
);

CREATE TABLE IF NOT EXISTS cycle_medications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL REFERENCES treatment_cycles(id) ON DELETE CASCADE,
    drug_name TEXT NOT NULL,
    planned_dose REAL NOT NULL CHECK (planned_dose >= 0),
    actual_dose REAL CHECK (actual_dose >= 0),
    dose_unit TEXT NOT NULL,
    route TEXT,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned', 'given', 'modified', 'held')),
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cycle_meds_cycle ON cycle_medications(cycle_id);

CREATE TABLE IF NOT EXISTS treatment_cycle_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL REFERENCES treatment_cycles(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    UNIQUE (cycle_id, revision_number)
);

CREATE TABLE IF NOT EXISTS cycle_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL REFERENCES treatment_cycles(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL,
    reason_category TEXT,
    rationale TEXT,
    evidence_json TEXT,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_cycle ON cycle_decisions(cycle_id);

CREATE TABLE IF NOT EXISTS lab_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    cycle_id INTEGER REFERENCES treatment_cycles(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    content_type TEXT,
    stored_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    extraction_method TEXT,
    extraction_note TEXT,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lab_investigations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    cycle_id INTEGER NOT NULL REFERENCES treatment_cycles(id) ON DELETE CASCADE,
    test_name TEXT NOT NULL,
    test_code TEXT,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    ref_low REAL,
    ref_high REAL,
    collection_date TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'verified'
        CHECK (verification_status IN ('unverified', 'verified', 'rejected')),
    source TEXT NOT NULL DEFAULT 'manual',
    source_document TEXT,
    verified_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_labs_patient_test_date
    ON lab_investigations(patient_id, test_name, collection_date);
CREATE INDEX IF NOT EXISTS idx_labs_cycle ON lab_investigations(cycle_id);

CREATE TABLE IF NOT EXISTS symptoms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    cycle_id INTEGER REFERENCES treatment_cycles(id) ON DELETE SET NULL,
    ctcae_term TEXT NOT NULL,
    ctcae_version TEXT NOT NULL DEFAULT '5.0',
    grade INTEGER NOT NULL CHECK (grade BETWEEN 1 AND 5),
    onset_date TEXT NOT NULL,
    resolution_date TEXT,
    status TEXT NOT NULL DEFAULT 'ongoing'
        CHECK (status IN ('ongoing', 'improving', 'resolved')),
    note TEXT,
    suspected_drug TEXT,
    confirmed_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (resolution_date IS NULL OR resolution_date >= onset_date)
);

CREATE INDEX IF NOT EXISTS idx_symptoms_patient_date
    ON symptoms(patient_id, onset_date);
CREATE INDEX IF NOT EXISTS idx_symptoms_cycle ON symptoms(cycle_id);

CREATE TABLE IF NOT EXISTS portal_access (
    patient_id INTEGER PRIMARY KEY REFERENCES patients(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    last_login_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portal_sessions (
    token_hash TEXT PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patient_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    cycle_id INTEGER REFERENCES treatment_cycles(id) ON DELETE SET NULL,
    text TEXT NOT NULL,
    input_mode TEXT NOT NULL DEFAULT 'text' CHECK (input_mode IN ('text', 'voice')),
    language TEXT,
    onset_date TEXT,
    symptoms_json TEXT,
    daily_impact TEXT CHECK (daily_impact IS NULL OR daily_impact IN ('none', 'some', 'selfcare')),
    temperature REAL,
    audio_path TEXT,
    audio_type TEXT,
    red_flags_json TEXT,
    ai_json TEXT,
    ai_status TEXT NOT NULL DEFAULT 'pending',
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'reviewed', 'recorded', 'dismissed')),
    doctor_note TEXT,
    patient_message TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    symptom_ids_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_patient_reports_patient ON patient_reports(patient_id, status);

CREATE TABLE IF NOT EXISTS clinicians (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    specialty TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    last_login_at TEXT,
    created_at TEXT NOT NULL
);

-- Radiation oncology -------------------------------------------------------
CREATE TABLE IF NOT EXISTS rt_courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    site TEXT NOT NULL,
    site_group TEXT NOT NULL DEFAULT 'other',
    laterality TEXT,
    diagnosis TEXT,
    stage TEXT,
    histopathology TEXT,
    intent TEXT,
    machine TEXT,
    consultant TEXT,
    referred_by TEXT,
    chemo_plan_id INTEGER REFERENCES treatment_plans(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned', 'active', 'on_hold', 'completed', 'discontinued')),
    start_date TEXT,
    treatment_days TEXT NOT NULL DEFAULT '1,2,3,4,5',
    baseline_weight_kg REAL,
    setup_json TEXT,
    notes TEXT,
    approved_physicist TEXT,
    approved_oncologist TEXT,
    approved_at TEXT,
    completion_json TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rt_courses_patient ON rt_courses(patient_id);

CREATE TABLE IF NOT EXISTS rt_phases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES rt_courses(id) ON DELETE CASCADE,
    phase_number INTEGER NOT NULL CHECK (phase_number > 0),
    name TEXT,
    technique TEXT,
    energy TEXT,
    target TEXT,
    dose_per_fraction_cgy REAL NOT NULL CHECK (dose_per_fraction_cgy > 0),
    fractions_planned INTEGER NOT NULL CHECK (fractions_planned > 0),
    shift_x REAL, shift_y REAL, shift_z REAL,
    couch_movement TEXT,
    ssd_json TEXT,
    remarks TEXT,
    UNIQUE (course_id, phase_number)
);

CREATE TABLE IF NOT EXISTS rt_constraints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES rt_courses(id) ON DELETE CASCADE,
    structure TEXT NOT NULL,
    constraint_text TEXT,
    achieved_text TEXT,
    met TEXT CHECK (met IS NULL OR met IN ('yes', 'no', 'na')),
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rt_fractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES rt_courses(id) ON DELETE CASCADE,
    phase_id INTEGER NOT NULL REFERENCES rt_phases(id) ON DELETE CASCADE,
    treatment_date TEXT NOT NULL,
    dose_cgy REAL NOT NULL CHECK (dose_cgy > 0),
    mu REAL,
    therapist TEXT,
    note TEXT,
    extra INTEGER NOT NULL DEFAULT 0 CHECK (extra IN (0, 1)),
    recorded_by TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rt_fractions_course ON rt_fractions(course_id, treatment_date);

CREATE TABLE IF NOT EXISTS rt_breaks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES rt_courses(id) ON DELETE CASCADE,
    break_date TEXT NOT NULL,
    category TEXT NOT NULL,
    reason TEXT,
    recorded_by TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (course_id, break_date)
);

CREATE TABLE IF NOT EXISTS rt_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES rt_courses(id) ON DELETE CASCADE,
    week_number INTEGER NOT NULL CHECK (week_number > 0),
    review_date TEXT NOT NULL,
    weight_kg REAL,
    notes TEXT,
    decision TEXT NOT NULL DEFAULT 'continue',
    reason_category TEXT,
    rationale TEXT,
    reviewed_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rt_reviews_course ON rt_reviews(course_id, review_date);

CREATE TABLE IF NOT EXISTS rt_review_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL REFERENCES rt_reviews(id) ON DELETE CASCADE,
    row_key TEXT NOT NULL,
    ctcae_term TEXT NOT NULL,
    grade INTEGER NOT NULL CHECK (grade BETWEEN 0 AND 5),
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_rt_review_items_review ON rt_review_items(review_id);

CREATE TABLE IF NOT EXISTS rt_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES rt_courses(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail_json TEXT,
    actor TEXT,
    created_at TEXT NOT NULL
);

-- Surgical oncology ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS surgical_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    procedure_planned TEXT NOT NULL,
    indication TEXT,
    site TEXT,
    laterality TEXT,
    intent TEXT,
    urgency TEXT NOT NULL DEFAULT 'elective',
    surgeon TEXT,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned', 'scheduled', 'completed', 'discharged', 'cancelled')),
    surgery_date TEXT,
    admission_date TEXT,
    discharge_date TEXT,
    preop_json TEXT,
    operation_json TEXT,
    pathology_json TEXT,
    discharge_json TEXT,
    notes TEXT,
    cancel_reason TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_surgical_cases_patient ON surgical_cases(patient_id);

CREATE TABLE IF NOT EXISTS surgical_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES surgical_cases(id) ON DELETE CASCADE,
    event_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    clavien_dindo TEXT,
    complication TEXT,
    drain_output_ml REAL,
    pain_score INTEGER,
    author TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_surgical_events_case ON surgical_events(case_id, event_date);

-- Notes, documents, summaries -------------------------------------------------
CREATE TABLE IF NOT EXISTS clinical_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    note_date TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'progress',
    text TEXT NOT NULL,
    author TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_patient ON clinical_notes(patient_id, note_date);

CREATE TABLE IF NOT EXISTS patient_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    doc_type TEXT NOT NULL DEFAULT 'other',
    title TEXT NOT NULL,
    doc_date TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT,
    stored_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    text_excerpt TEXT,
    ai_summary TEXT,
    summary_method TEXT,
    note TEXT,
    uploaded_by TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_patient ON patient_documents(patient_id, doc_date);

CREATE TABLE IF NOT EXISTS patient_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    method TEXT NOT NULL,
    model TEXT,
    source_count INTEGER NOT NULL DEFAULT 0,
    generated_by TEXT,
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discharge_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'discharge',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'final', 'superseded')),
    version INTEGER NOT NULL DEFAULT 1,
    parent_id INTEGER REFERENCES discharge_summaries(id) ON DELETE SET NULL,
    period_from TEXT,
    period_to TEXT,
    content_json TEXT NOT NULL,
    sources_json TEXT,
    generated_method TEXT,
    created_by TEXT,
    finalised_by TEXT,
    finalised_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discharge_patient ON discharge_summaries(patient_id, status);

-- WhatsApp ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS whatsapp_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER REFERENCES patients(id) ON DELETE CASCADE,
    wa_from TEXT,
    direction TEXT NOT NULL CHECK (direction IN ('out', 'in')),
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    template_name TEXT,
    template_params_json TEXT,
    buttons_json TEXT,
    media_path TEXT,
    media_type TEXT,
    status TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'simulator',
    wa_message_id TEXT,
    error TEXT,
    related_type TEXT,
    related_id INTEGER,
    report_id INTEGER REFERENCES patient_reports(id) ON DELETE SET NULL,
    scheduled_for TEXT,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wa_patient ON whatsapp_messages(patient_id, created_at);
CREATE INDEX IF NOT EXISTS idx_wa_due ON whatsapp_messages(status, scheduled_for);

CREATE TABLE IF NOT EXISTS whatsapp_sessions (
    patient_id INTEGER PRIMARY KEY REFERENCES patients(id) ON DELETE CASCADE,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clinician_sessions (
    token_hash TEXT PRIMARY KEY,
    clinician_id INTEGER NOT NULL REFERENCES clinicians(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

# Columns added after the first milestone. Applied idempotently to existing databases.
ADDED_COLUMNS = {
    "treatment_plans": [("planned_cycles", "INTEGER"), ("cycle_interval_days", "INTEGER"), ("notes", "TEXT")],
    "treatment_cycles": [("reason_category", "TEXT"), ("recorded_by", "TEXT"), ("recorded_at", "TEXT")],
    "lab_investigations": [("document_id", "INTEGER REFERENCES lab_documents(id) ON DELETE SET NULL")],
    "lab_documents": [("recording_date", "TEXT"), ("report_date", "TEXT")],
    "patients": [("phone", "TEXT"), ("preferred_language", "TEXT"), ("wa_opt_in", "INTEGER NOT NULL DEFAULT 0"),
                 ("wa_opt_in_at", "TEXT"), ("wa_opt_in_method", "TEXT")],
    "patient_reports": [("checkin_json", "TEXT"), ("ui_language", "TEXT"), ("source", "TEXT NOT NULL DEFAULT 'portal'")],
    "whatsapp_messages": [("meta_json", "TEXT")],
}

# Development clinician account (password comes from ONCOCARE_DEV_PASSWORD until OTP login is added).
DEV_CLINICIANS = [("Dr. R. Khanna", "8008475309", "Medical oncology")]
DEMO_PHONES = {"HN-24018": "9000000018", "HN-24031": "9000000031", "HN-24077": "9000000077", "HN-24102": "9000000102"}


def normalise_phone(value: object) -> str | None:
    """Digits only; drops a leading 0 or India country code so 08008475309 / +91 80084 75309 / 8008475309 match."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits or None

CLINICIAN = "Dr. R. Khanna"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def dict_factory(cursor: sqlite3.Cursor, row: sqlite3.Row) -> dict[str, Any]:
    return {column[0]: row[index] for index, column in enumerate(cursor.description)}


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    connection.row_factory = dict_factory
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def migrate(db_path: str | Path) -> None:
    connection = connect(db_path)
    try:
        connection.executescript(SCHEMA)
        for table, columns in ADDED_COLUMNS.items():
            existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, definition in columns:
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_patients_phone ON patients(phone) WHERE phone IS NOT NULL")
        for name, phone, specialty in DEV_CLINICIANS:
            connection.execute(
                "INSERT OR IGNORE INTO clinicians (name, phone, specialty, active, created_at) VALUES (?, ?, ?, 1, ?)",
                (name, phone, specialty, utc_now()))
        # Normalise legacy cycle statuses and backfill per-drug rows for single-drug cycles.
        connection.execute("UPDATE treatment_cycles SET status = 'given' WHERE status IN ('administered', 'completed')")
        legacy = connection.execute(
            """SELECT c.* FROM treatment_cycles c
               WHERE NOT EXISTS (SELECT 1 FROM cycle_medications m WHERE m.cycle_id = c.id)"""
        ).fetchall()
        for cycle in legacy:
            given = cycle["status"] in ("given", "modified")
            med_status = "planned"
            if cycle["status"] == "held":
                med_status = "held"
            elif given:
                changed = cycle["actual_dose"] is not None and abs(cycle["actual_dose"] - cycle["planned_dose"]) > 1e-9
                med_status = "modified" if changed else "given"
            connection.execute(
                """INSERT INTO cycle_medications (cycle_id, drug_name, planned_dose, actual_dose, dose_unit, route, status, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                (cycle["id"], cycle["drug_name"], cycle["planned_dose"], cycle["actual_dose"], cycle["dose_unit"], cycle["route"], med_status),
            )
        plans = connection.execute(
            """SELECT p.id FROM treatment_plans p
               WHERE NOT EXISTS (SELECT 1 FROM plan_medications m WHERE m.treatment_plan_id = p.id)"""
        ).fetchall()
        for plan in plans:
            first = connection.execute(
                """SELECT m.* FROM cycle_medications m JOIN treatment_cycles c ON c.id = m.cycle_id
                   WHERE c.treatment_plan_id = ? ORDER BY c.cycle_number, m.sort_order""", (plan["id"],)
            ).fetchall()
            seen: set[str] = set()
            for index, med in enumerate(first):
                if med["drug_name"] in seen:
                    continue
                seen.add(med["drug_name"])
                connection.execute(
                    "INSERT INTO plan_medications (treatment_plan_id, drug_name, dose, dose_unit, route, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                    (plan["id"], med["drug_name"], med["planned_dose"], med["dose_unit"], med["route"], index),
                )
        connection.execute(
            """UPDATE treatment_plans SET planned_cycles = (SELECT COUNT(*) FROM treatment_cycles c WHERE c.treatment_plan_id = treatment_plans.id)
               WHERE planned_cycles IS NULL"""
        )
        connection.commit()
    finally:
        connection.close()


@contextmanager
def transaction(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    connection = connect(db_path)
    try:
        connection.execute("BEGIN")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def row_or_none(connection: sqlite3.Connection, sql: str, values: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    return connection.execute(sql, values).fetchone()


def rows(connection: sqlite3.Connection, sql: str, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return connection.execute(sql, values).fetchall()


def record_cycle_revision(connection: sqlite3.Connection, cycle_id: int) -> None:
    cycle = row_or_none(connection, "SELECT * FROM treatment_cycles WHERE id = ?", (cycle_id,))
    if not cycle:
        return
    cycle["medications"] = rows(connection, "SELECT * FROM cycle_medications WHERE cycle_id = ? ORDER BY sort_order, id", (cycle_id,))
    revision = connection.execute(
        "SELECT COALESCE(MAX(revision_number), 0) + 1 AS next_revision FROM treatment_cycle_revisions WHERE cycle_id = ?",
        (cycle_id,),
    ).fetchone()["next_revision"]
    connection.execute(
        "INSERT INTO treatment_cycle_revisions (cycle_id, revision_number, snapshot_json, changed_at) VALUES (?, ?, ?, ?)",
        (cycle_id, revision, json.dumps(cycle, separators=(",", ":")), utc_now()),
    )


def sync_cycle_summary(connection: sqlite3.Connection, cycle_id: int) -> None:
    """Keep the legacy single-drug columns on treatment_cycles in step with cycle_medications."""
    meds = rows(connection, "SELECT * FROM cycle_medications WHERE cycle_id = ? ORDER BY sort_order, id", (cycle_id,))
    if not meds:
        return
    first = meds[0]
    connection.execute(
        "UPDATE treatment_cycles SET drug_name = ?, planned_dose = ?, actual_dose = ?, dose_unit = ?, route = ? WHERE id = ?",
        (" + ".join(m["drug_name"] for m in meds), first["planned_dose"], first["actual_dose"], first["dose_unit"], first["route"], cycle_id),
    )


# ---------------------------------------------------------------------------
# Synthetic demo data
# ---------------------------------------------------------------------------

def _insert_plan(connection, patient_id, regimen, intent, start, interval, medications, cycles, now):
    plan_id = connection.execute(
        """INSERT INTO treatment_plans (patient_id, regimen_name, intent, status, start_date, planned_cycles,
               cycle_interval_days, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?)""",
        (patient_id, regimen, intent, start, len(cycles), interval, now, now),
    ).lastrowid
    for index, (drug, dose, unit, route) in enumerate(medications):
        connection.execute(
            "INSERT INTO plan_medications (treatment_plan_id, drug_name, dose, dose_unit, route, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
            (plan_id, drug, dose, unit, route, index),
        )
    cycle_ids = []
    for spec in cycles:
        number, planned, administered, status, actuals, reason, category = spec
        first = medications[0]
        cycle_id = connection.execute(
            """INSERT INTO treatment_cycles (treatment_plan_id, cycle_number, planned_date, administered_date, status,
                   drug_name, planned_dose, dose_unit, route, modification_reason, reason_category, recorded_by, recorded_at,
                   created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (plan_id, number, planned, administered, status, first[0], first[1], first[2], first[3], reason, category,
             CLINICIAN if status != "planned" else None, now if status != "planned" else None, now, now),
        ).lastrowid
        for index, (drug, dose, unit, route) in enumerate(medications):
            actual = actuals[index] if actuals else None
            if status == "planned":
                med_status = "planned"
            elif actual is None:
                med_status = "held"
            else:
                med_status = "given" if abs(actual - dose) < 1e-9 else "modified"
            connection.execute(
                """INSERT INTO cycle_medications (cycle_id, drug_name, planned_dose, actual_dose, dose_unit, route, status, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (cycle_id, drug, dose, actual, unit, route, med_status, index),
            )
        sync_cycle_summary(connection, cycle_id)
        if status != "planned":
            connection.execute(
                """INSERT INTO cycle_decisions (cycle_id, outcome, reason_category, rationale, decided_by, decided_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (cycle_id, status, category, reason, CLINICIAN, now),
            )
        cycle_ids.append(cycle_id)
    return plan_id, cycle_ids


def _insert_labs(connection, patient_id, cycle_ids, dates, series, now):
    from .reference import LAB_CATALOG
    for test_name, values in series.items():
        info = LAB_CATALOG[test_name]
        for index, value in enumerate(values):
            if value is None:
                continue
            connection.execute(
                """INSERT INTO lab_investigations (patient_id, cycle_id, test_name, value, unit, ref_low, ref_high,
                       collection_date, verification_status, source, verified_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'verified', 'manual', ?, ?, ?)""",
                (patient_id, cycle_ids[index], test_name, value, info["unit"], info["low"], info["high"], dates[index], CLINICIAN, now, now),
            )


def _seed_ananya(connection: sqlite3.Connection, now: str) -> None:
    # 1) Weekly cisplatin with chemoradiation: single drug, cycle 4 awaiting review.
    patient_id = connection.execute(
        """INSERT INTO patients (medical_record_number, first_name, last_name, date_of_birth, sex, diagnosis, active, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        ("HN-24018", "Ananya", "Mehta", "1980-04-18", "female", "Locally advanced cervical cancer (FIGO IIIB)", now, now),
    ).lastrowid
    _, cycle_ids = _insert_plan(
        connection, patient_id, "Weekly cisplatin with radiotherapy", "curative", "2026-09-08", 7,
        [("Cisplatin", 40, "mg/m2", "IV infusion")],
        [
            (1, "2026-09-08", "2026-09-08", "given", [40], None, None),
            (2, "2026-09-15", "2026-09-15", "given", [40], None, None),
            (3, "2026-09-22", "2026-09-22", "given", [40], "Grade 1 neuropathy noted; proceed at full dose with close follow-up.", "Toxicity"),
            (4, "2026-09-29", None, "planned", None, None, None),
            (5, "2026-10-06", None, "planned", None, None, None),
            (6, "2026-10-13", None, "planned", None, None, None),
        ],
        now,
    )
    _insert_labs(connection, patient_id, cycle_ids, ["2026-09-07", "2026-09-14", "2026-09-21", "2026-09-25"], {
        "Hemoglobin": [12.4, 11.8, 11.2, 10.6],
        "WBC": [6.8, 5.9, 4.6, 3.8],
        "ANC": [4200, 3100, 2200, 1650],
        "Platelets": [245, 211, 176, 145],
        "Creatinine": [0.8, 0.9, 1.0, 1.3],
        "Urea": [24, 28, 31, 38],
        "Sodium": [139, 138, 137, 136],
        "Potassium": [4.2, 4.0, 3.8, 3.6],
        "Magnesium": [2.0, 1.9, 1.7, 1.5],
        "AST": [22, None, 24, 28],
        "ALT": [18, None, 20, 26],
        "Bilirubin": [0.6, None, 0.6, 0.7],
    }, now)
    connection.executemany(
        """INSERT INTO symptoms (patient_id, cycle_id, ctcae_term, grade, onset_date, resolution_date, status, note,
               suspected_drug, confirmed_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Cisplatin', ?, ?, ?)""",
        [
            (patient_id, cycle_ids[1], "Nausea", 2, "2026-09-09", "2026-09-12", "resolved", "Reduced oral intake for 2 days; settled with ondansetron.", CLINICIAN, now, now),
            (patient_id, cycle_ids[2], "Nausea", 1, "2026-09-16", None, "improving", "Mild; eating normally.", CLINICIAN, now, now),
            (patient_id, cycle_ids[2], "Peripheral sensory neuropathy", 1, "2026-09-18", None, "ongoing", "Fingertip paresthesia, no functional limitation.", CLINICIAN, now, now),
            (patient_id, cycle_ids[3], "Peripheral sensory neuropathy", 2, "2026-09-24", None, "ongoing", "Difficulty buttoning clothes; limits instrumental ADL.", CLINICIAN, now, now),
            (patient_id, cycle_ids[3], "Tinnitus", 1, "2026-09-23", None, "ongoing", "Intermittent high-pitched ringing, no intervention.", CLINICIAN, now, now),
        ],
    )


def _seed_rahul(connection: sqlite3.Connection, now: str) -> None:
    # 2) Three-weekly paclitaxel + carboplatin: multi-drug, cycle 2 modified, cycle 3 labs missing.
    patient_id = connection.execute(
        """INSERT INTO patients (medical_record_number, first_name, last_name, date_of_birth, sex, diagnosis, active, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        ("HN-24031", "Rahul", "Iyer", "1966-11-02", "male", "Stage IVA non-small cell lung cancer (adenocarcinoma)", now, now),
    ).lastrowid
    _, cycle_ids = _insert_plan(
        connection, patient_id, "Paclitaxel + carboplatin q21d", "palliative", "2026-08-20", 21,
        [("Paclitaxel", 175, "mg/m2", "IV infusion"), ("Carboplatin", 5, "AUC", "IV infusion")],
        [
            (1, "2026-08-20", "2026-08-20", "given", [175, 5], None, None),
            (2, "2026-09-10", "2026-09-17", "modified", [135, 5],
             "ANC 1,200/uL (G2) on 09 Sep: delayed 1 week. Paclitaxel reduced to 135 mg/m2 for G2 sensory neuropathy.", "Toxicity"),
            (3, "2026-10-08", None, "planned", None, None, None),
            (4, "2026-10-29", None, "planned", None, None, None),
        ],
        now,
    )
    _insert_labs(connection, patient_id, [cycle_ids[0], cycle_ids[1], cycle_ids[1]], ["2026-08-19", "2026-09-09", "2026-09-16"], {
        "Hemoglobin": [13.6, 12.4, 12.1], "WBC": [7.4, 3.2, 4.1], "ANC": [4900, 1200, 1900], "Platelets": [262, 190, 168],
        "Creatinine": [0.9, None, 1.0], "Sodium": [138, None, 136], "Potassium": [4.3, None, 4.1], "Magnesium": [2.1, None, 1.8],
        "AST": [26, None, 31], "ALT": [22, None, 35], "Bilirubin": [0.5, None, 0.6],
    }, now)
    connection.executemany(
        """INSERT INTO symptoms (patient_id, cycle_id, ctcae_term, grade, onset_date, resolution_date, status, note,
               suspected_drug, confirmed_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (patient_id, cycle_ids[1], "Peripheral sensory neuropathy", 2, "2026-09-02", None, "ongoing", "Numb feet, difficulty with fine tasks.", "Paclitaxel", CLINICIAN, now, now),
            (patient_id, cycle_ids[1], "Alopecia", 2, "2026-09-05", None, "ongoing", "Uses head covering.", "Paclitaxel", CLINICIAN, now, now),
            (patient_id, cycle_ids[1], "Myalgia", 1, "2026-08-23", "2026-08-26", "resolved", None, "Paclitaxel", CLINICIAN, now, now),
            (patient_id, cycle_ids[2], "Fatigue", 2, "2026-09-22", None, "ongoing", "Not relieved by rest; needs help with shopping.", None, CLINICIAN, now, now),
        ],
    )


def _seed_priya(connection: sqlite3.Connection, now: str) -> None:
    # 3) Three-weekly cisplatin 75 mg/m2 with no labs yet: upload sample_reports/*.pdf to see extraction and trends.
    patient_id = connection.execute(
        """INSERT INTO patients (medical_record_number, first_name, last_name, date_of_birth, sex, diagnosis, active, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        ("HN-24077", "Priya", "Raman", "1974-02-11", "female", "Squamous cell carcinoma of the oropharynx, stage III (synthetic)", now, now),
    ).lastrowid
    _insert_plan(
        connection, patient_id, "Cisplatin 75 mg/m2 q21d", "curative", "2026-08-04", 21,
        [("Cisplatin", 75, "mg/m2", "IV infusion")],
        [
            (1, "2026-08-04", "2026-08-04", "given", [75], None, None),
            (2, "2026-08-25", "2026-08-25", "given", [75], None, None),
            (3, "2026-09-15", "2026-09-15", "given", [75], None, None),
            (4, "2026-10-06", None, "planned", None, None, None),
            (5, "2026-10-27", None, "planned", None, None, None),
            (6, "2026-11-17", None, "planned", None, None, None),
        ],
        now,
    )


def _rt_course(connection, patient_id, now, *, site, site_group, laterality, diagnosis, stage, histopathology, intent, machine,
               consultant, treatment_days, setup, phases, constraints, start, chemo_plan_id=None, baseline_weight=None):
    from datetime import date as _date, timedelta as _td
    approved_at = (_date.fromisoformat(start) - _td(days=3)).isoformat() + "T10:30:00+00:00"
    course_id = connection.execute(
        """INSERT INTO rt_courses (patient_id, site, site_group, laterality, diagnosis, stage, histopathology, intent, machine, consultant,
               chemo_plan_id, status, start_date, treatment_days, baseline_weight_kg, setup_json, approved_physicist, approved_oncologist,
               approved_at, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (patient_id, site, site_group, laterality, diagnosis, stage, histopathology, intent, machine, consultant, chemo_plan_id, start,
         treatment_days, baseline_weight, json.dumps(setup), "S. Iyer", consultant, approved_at, consultant, now, now)).lastrowid
    phase_ids = []
    for number, (name, technique, energy, target, dose, fractions, shift, ssd) in enumerate(phases, start=1):
        phase_ids.append(connection.execute(
            """INSERT INTO rt_phases (course_id, phase_number, name, technique, energy, target, dose_per_fraction_cgy, fractions_planned,
                   shift_x, shift_y, shift_z, ssd_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (course_id, number, name, technique, energy, target, dose, fractions, *(shift or (None, None, None)),
             json.dumps(ssd) if ssd else None)).lastrowid)
    for order, (structure, constraint, achieved, met) in enumerate(constraints):
        connection.execute("INSERT INTO rt_constraints (course_id, structure, constraint_text, achieved_text, met, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                           (course_id, structure, constraint, achieved, met, order))
    connection.execute("INSERT INTO rt_events (course_id, kind, summary, actor, created_at) VALUES (?, 'approved', ?, ?, ?)",
                       (course_id, f"Plan approved by {consultant} (radiation oncologist) and S. Iyer (physicist)", consultant, approved_at))
    return course_id, phase_ids


def _rt_fractions(connection, course_id, phase_ids, phases, dates, mus, now):
    """Deliver fractions on `dates` in phase order."""
    index = 0
    for phase_id, phase, mu in zip(phase_ids, phases, mus):
        for _ in range(phase[5]):
            if index >= len(dates):
                return
            connection.execute(
                """INSERT INTO rt_fractions (course_id, phase_id, treatment_date, dose_cgy, mu, therapist, extra, recorded_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""", (course_id, phase_id, dates[index], phase[4], mu, ("MK", "SR", "AV")[index % 3], "RTT", now))
            index += 1


def _rt_review(connection, course_id, week, when, weight, items, decision, rationale, consultant, now, reason=None):
    review_id = connection.execute(
        """INSERT INTO rt_reviews (course_id, week_number, review_date, weight_kg, decision, reason_category, rationale, reviewed_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (course_id, week, when, weight, decision, reason, rationale, consultant, now, now)).lastrowid
    for row_key, term, grade, note in items:
        connection.execute("INSERT INTO rt_review_items (review_id, row_key, ctcae_term, grade, note) VALUES (?, ?, ?, ?, ?)",
                           (review_id, row_key, term, grade, note))


def _treatment_dates(start, count, weekdays, skip=()):
    from datetime import date as _date, timedelta as _td
    out, probe = [], _date.fromisoformat(start)
    while len(out) < count:
        if probe.isoweekday() in weekdays and probe.isoformat() not in skip:
            out.append(probe.isoformat())
        probe += _td(days=1)
    return out


def _seed_lakshmi(connection: sqlite3.Connection, now: str) -> None:
    # 4) Adjuvant breast radiotherapy, modelled on a paper RT case sheet: 40.5 Gy/15# VMAT + 10 Gy/5# boost, Mon-Sat.
    #    Dates are relative to the seeding day so the course is always "in progress": 19 fractions given, one public
    #    holiday missed, the last boost fraction due on the next treatment day.
    from datetime import date as _date, timedelta as _td
    patient_id = connection.execute(
        """INSERT INTO patients (medical_record_number, first_name, last_name, date_of_birth, sex, diagnosis, active, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        ("HN-24102", "Lakshmi", "Varma", "1975-06-03", "female", "Carcinoma right breast, post breast-conserving surgery (synthetic)", now, now),
    ).lastrowid
    weekdays = {1, 2, 3, 4, 5, 6}
    days, probe = [], _date.today() - _td(days=1)
    while len(days) < 20:
        if probe.isoweekday() in weekdays:
            days.append(probe)
        probe -= _td(days=1)
    days.reverse()
    holiday = days[13].isoformat()
    dates = [d.isoformat() for d in days if d.isoformat() != holiday]
    phases = [("Phase I · whole breast + SCF", "VMAT", "6MV", "Right breast and supraclavicular fossa", 270, 15, (11.81, 10.10, 9.8),
               {"tps": {"ap": 85.8}, "actual": {"ap": 85.8}}),
              ("Phase II · tumour bed boost", "Electrons", "10MeV", "Tumour bed (en face electrons)", 200, 5, None, None)]
    course_id, phase_ids = _rt_course(
        connection, patient_id, now, site="Right breast + supraclavicular fossa", site_group="breast", laterality="right",
        diagnosis="Carcinoma right breast, post breast-conserving surgery", stage="pT2 N1a M0", histopathology="Invasive ductal carcinoma, grade 2",
        intent="curative", machine="Elekta Versa HD", consultant="Dr. P. Rao", treatment_days="1,2,3,4,5,6",
        setup={"head_rest": "B", "arm_position": "Above head", "immobilisation": "Breast board", "bolus_cm": "None"},
        phases=phases, start=dates[0], baseline_weight=83.0,
        constraints=[("PTV breast", "D95 ≥ 95%", "96.4%", "yes"), ("Ipsilateral lung", "V16Gy < 25%", "19.8%", "yes"),
                     ("Heart", "Mean < 3 Gy", "1.1 Gy", "yes"), ("Contralateral breast", "Mean < 3 Gy", "1.6 Gy", "yes"),
                     ("Spinal cord", "Dmax < 30 Gy", "11.9 Gy", "yes"), ("Thyroid", "Mean as low as possible", "9.4 Gy", "na")])
    _rt_fractions(connection, course_id, phase_ids, phases, dates, (498.4, 212.0), now)
    connection.execute("INSERT INTO rt_breaks (course_id, break_date, category, reason, recorded_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (course_id, holiday, "Public holiday", "Department closed", "RTT", now))
    first = _date.fromisoformat(dates[0])
    reviews = [
        (1, first + _td(days=1), 83.0, [("skin", "Dermatitis radiation", 0, None), ("general", "Fatigue", 0, None)], "continue", None),
        (2, first + _td(days=8), 83.0, [("skin", "Dermatitis radiation", 0, "Mild pinkness only"), ("general", "Fatigue", 0, None)], "continue", None),
        (3, first + _td(days=14), 82.0, [("skin", "Dermatitis radiation", 1, "Faint erythema over breast"), ("general", "Fatigue", 1, None)], "continue", None),
        (4, first + _td(days=21), 82.0, [("skin", "Dermatitis radiation", 2, "Patchy moist desquamation in inframammary fold"),
                                          ("general", "Fatigue", 1, None), ("general", "Breast pain", 1, "Tender at fold")],
         "continue_supportive", "Moist desquamation confined to the fold: hydrogel dressing, keep fold dry, review in 3 days."),
    ]
    for week, when, weight, items, decision, rationale in reviews:
        if when < _date.today():
            _rt_review(connection, course_id, week, when.isoformat(), weight, items, decision, rationale, "Dr. P. Rao", now)
    _seed_lakshmi_surgery(connection, patient_id, first, now)
    connection.execute("INSERT INTO clinical_notes (patient_id, note_date, category, text, author, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (patient_id, (first - _td(days=6)).isoformat(), "progress",
                        "Planning CT done on breast board, arms above head. Post-BCS (right) with clear margins; ER/PR positive, HER2 negative. "
                        "Plan: hypofractionated whole breast + SCF 40.5 Gy/15# VMAT followed by tumour bed boost 10 Gy/5# electrons. "
                        "Letrozole to start after radiotherapy.", "Dr. P. Rao", now, now))


def _seed_lakshmi_surgery(connection: sqlite3.Connection, patient_id: int, rt_start, now: str) -> None:
    """Breast-conserving surgery + sentinel node biopsy about 8 weeks before radiotherapy (from the same synthetic case)."""
    from datetime import timedelta as _td
    op = rt_start - _td(days=56)
    d = lambda n: (op + _td(days=n)).isoformat()  # noqa: E731
    preop = {"checks": {k: True for k in ("consent", "anaesthesia_fitness", "imaging_reviewed", "mdt", "blood_arranged", "site_marked", "fasting_instructions")},
             "asa": "II", "comorbidities": "Hypothyroidism on levothyroxine", "investigations": "CBC, RFT, LFT, ECG, chest X-ray normal; mammogram + USG right breast 2.6 cm UOQ mass",
             "fasting_from": "10 pm the night before", "special_instructions": "Wire localisation of the lump on the morning of surgery"}
    operation = {"operation_date": d(0), "procedure_performed": "Right breast-conserving surgery (wide local excision) + sentinel lymph node biopsy",
                 "approach": "Open", "anaesthesia": "General", "surgeon": "Dr. A. Menon", "assistants": "Dr. K. Reddy", "anaesthetist": "Dr. S. Gupta",
                 "incision": "Curvilinear incision right upper outer quadrant; separate axillary incision",
                 "findings": "2.8 cm firm lump right UOQ, wire tip within lesion; 3 blue/hot sentinel nodes, one enlarged",
                 "frozen_section": "Sentinel nodes: 1 of 3 with metastatic carcinoma; margins grossly clear; cavity shaves taken",
                 "specimens": "WLE (oriented, clips), 4 cavity shaves, 3 sentinel nodes", "ebl_ml": 80, "transfusion_units": 0,
                 "drains": "One 14 Fr suction drain, axilla", "duration_min": 95, "intraop_events": "None",
                 "signed_by": "Dr. A. Menon", "signed_at": now}
    pathology = {"report_date": d(8), "histology": "Invasive ductal carcinoma (NST)", "grade": "2", "size_mm": 28, "pt": "pT2", "pn": "pN1a",
                 "margins": "negative", "closest_margin_mm": 4, "nodes_examined": 3, "nodes_positive": 1, "lvi": "absent", "pni": "absent",
                 "biomarkers": "ER positive (90%), PR positive (60%), HER2 negative (IHC 1+), Ki-67 18%", "entered_by": "Dr. A. Menon", "entered_at": now}
    discharge = {"condition": "Afebrile, ambulant, tolerating diet; wound clean", "drains_at_discharge": "Axillary drain in situ (40 ml/day)",
                 "wound_care": "Keep dressing dry; drain diary", "suture_removal_date": d(10), "follow_up_date": d(7),
                 "instructions": "Arm exercises as shown by physiotherapy", "signed_by": "Dr. A. Menon", "signed_at": now}
    case_id = connection.execute(
        """INSERT INTO surgical_cases (patient_id, procedure_planned, indication, site, laterality, intent, urgency, surgeon, status, surgery_date,
               admission_date, discharge_date, preop_json, operation_json, pathology_json, discharge_json, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'curative', 'elective', ?, 'discharged', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (patient_id, "Right breast-conserving surgery + sentinel lymph node biopsy", "Carcinoma right breast, cT2 N0 (core biopsy: IDC)", "Right breast",
         "right", "Dr. A. Menon", d(0), d(-1), d(2), json.dumps(preop), json.dumps(operation), json.dumps(pathology), json.dumps(discharge),
         "Dr. A. Menon", now, now)).lastrowid
    events = [
        (d(-14), "plan", "Surgery planned: Right breast-conserving surgery + sentinel lymph node biopsy, scheduled " + d(0), None, None, None, None),
        (d(0), "operation", "Operation performed: Right breast-conserving surgery (wide local excision) + sentinel lymph node biopsy (Open)", None, None, None, None),
        (d(1), "progress", "POD 1: comfortable, afebrile, tolerating oral diet, mobilised. Wound dry.", None, None, 120, 3),
        (d(2), "progress", "POD 2: drain 40 ml serous. Shoulder exercises started. Fit for discharge with drain.", None, None, 40, 2),
        (d(2), "discharge", "Discharged on post-operative day 2: Afebrile, ambulant, tolerating diet; wound clean", None, None, None, None),
        (d(7), "drain", "Drain removed in clinic (output < 30 ml/day for 2 days).", None, None, 25, None),
        (d(8), "pathology", "Histopathology: Invasive ductal carcinoma (NST), pT2 pN1a, margins negative", None, None, None, None),
        (d(12), "complication", "Axillary seroma, 150 ml aspirated in clinic under aseptic precautions; no infection.", "I", "Seroma", None, 2),
    ]
    for day, kind, text, grade, comp, drain, pain in events:
        connection.execute("""INSERT INTO surgical_events (case_id, event_date, kind, text, clavien_dindo, complication, drain_output_ml, pain_score, author, created_at)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (case_id, day, kind, text, grade, comp, drain, pain, "Dr. A. Menon", now))
    connection.execute("INSERT INTO clinical_notes (patient_id, note_date, category, text, author, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (patient_id, d(-16), "tumour_board", "Tumour board: right breast IDC cT2 N0, suitable for breast conservation. Plan BCS + SLNB, "
                        "then adjuvant radiotherapy; endocrine therapy if hormone receptor positive.", "Dr. P. Rao", now, now))
    connection.execute("INSERT INTO clinical_notes (patient_id, note_date, category, text, author, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (patient_id, d(14), "tumour_board", "Post-op tumour board: pT2 pN1a (1/3 SLN), margins clear, ER/PR+, HER2−. Options discussed; "
                        "plan whole breast + SCF radiotherapy with tumour bed boost, then letrozole. Chemotherapy per genomic risk discussion with patient.",
                        "Dr. P. Rao", now, now))


def _seed_ananya_rt(connection: sqlite3.Connection, now: str) -> None:
    # Pelvic chemoradiation for the cervical cancer demo patient (completed course, linked to her weekly cisplatin plan).
    patient = connection.execute("SELECT id FROM patients WHERE medical_record_number = 'HN-24018'").fetchone()
    if not patient or connection.execute("SELECT 1 FROM rt_courses WHERE patient_id = ?", (patient["id"],)).fetchone():
        return
    plan = connection.execute("SELECT id FROM treatment_plans WHERE patient_id = ? ORDER BY id LIMIT 1", (patient["id"],)).fetchone()
    dates = _treatment_dates("2026-09-08", 25, {1, 2, 3, 4, 5}, skip=("2026-10-02",))
    phases = [("Phase I · pelvis", "IMRT", "6MV", "Primary, parametria and pelvic nodes", 200, 25, (0.4, -1.2, 2.1), {"tps": {"ap": 89.6}})]
    course_id, phase_ids = _rt_course(
        connection, patient["id"], now, site="Pelvis (cervix with pelvic nodes)", site_group="pelvis", laterality=None,
        diagnosis="Carcinoma cervix", stage="FIGO IIIB", histopathology="Squamous cell carcinoma", intent="curative", machine="Elekta Versa HD",
        consultant="Dr. P. Rao", treatment_days="1,2,3,4,5", setup={"immobilisation": "Knee-and-foot rest", "bladder": "Full", "arm_position": "On chest"},
        phases=phases, start=dates[0], chemo_plan_id=plan["id"] if plan else None, baseline_weight=58.0,
        constraints=[("PTV", "D95 ≥ 95%", "97.2%", "yes"), ("Bowel bag", "V45Gy < 195 cc", "171 cc", "yes"),
                     ("Bladder", "V45Gy < 35%", "29%", "yes"), ("Rectum", "V45Gy < 60%", "52%", "yes"), ("Femoral heads", "V30Gy < 15%", "8%", "yes")])
    _rt_fractions(connection, course_id, phase_ids, phases, dates, (612.5,), now)
    connection.execute("INSERT INTO rt_breaks (course_id, break_date, category, reason, recorded_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (course_id, "2026-10-02", "Public holiday", "Gandhi Jayanti", "RTT", now))
    for week, when, weight, items, decision, rationale in [
        (1, "2026-09-11", 58.0, [("lower_gi", "Diarrhea", 0, None), ("genitourinary", "Cystitis noninfective", 0, None)], "continue", None),
        (2, "2026-09-18", 57.5, [("lower_gi", "Diarrhea", 1, "3 stools/day"), ("genitourinary", "Cystitis noninfective", 0, None)], "continue", None),
        (3, "2026-09-25", 56.5, [("lower_gi", "Diarrhea", 2, "5 extra stools/day"), ("genitourinary", "Cystitis noninfective", 1, "Mild dysuria"),
                                 ("general", "Fatigue", 1, None)], "continue_supportive", "Loperamide, oral rehydration and low-residue diet; urine culture sent."),
        (4, "2026-10-01", 56.0, [("lower_gi", "Diarrhea", 1, "Settling"), ("genitourinary", "Cystitis noninfective", 1, None), ("general", "Fatigue", 1, None)], "continue", None),
        (5, "2026-10-09", 55.5, [("lower_gi", "Diarrhea", 1, None), ("genitourinary", "Cystitis noninfective", 0, None), ("general", "Fatigue", 1, None)], "continue", None),
    ]:
        _rt_review(connection, course_id, week, when, weight, items, decision, rationale, "Dr. P. Rao", now)
    completion = {"outcome": "completed_as_planned", "outcome_label": "Completed as planned", "start_date": dates[0], "end_date": dates[-1],
                  "fractions_delivered": 25, "fractions_planned": 25, "dose_delivered_cgy": 5000, "dose_planned_cgy": 5000,
                  "overall_treatment_days": 36, "missed_days": 1, "phases": [{"name": phases[0][0], "delivered": 25, "planned": 25,
                  "dose_delivered_cgy": 5000, "technique": "IMRT"}], "gap_correction": "not_needed",
                  "notes": "One-day public holiday gap; no compensation needed. Proceed to brachytherapy (separate record).",
                  "signed_by": "Dr. P. Rao", "signed_at": now}
    connection.execute("UPDATE rt_courses SET status = 'completed', completion_json = ? WHERE id = ?", (json.dumps(completion), course_id))
    connection.execute("INSERT INTO rt_events (course_id, kind, summary, actor, created_at) VALUES (?, 'completed', ?, ?, ?)",
                       (course_id, f"Completed as planned: 50 Gy in 25 fractions, {dates[0]} to {dates[-1]}", "Dr. P. Rao", now))


DEMO_PATIENTS = [("HN-24018", _seed_ananya), ("HN-24031", _seed_rahul), ("HN-24077", _seed_priya), ("HN-24102", _seed_lakshmi)]


def seed_demo(db_path: str | Path) -> None:
    """Add any synthetic demo patient whose MRN is not in the database yet."""
    with transaction(db_path) as connection:
        now = utc_now()
        for mrn, seed in DEMO_PATIENTS:
            if not connection.execute("SELECT 1 FROM patients WHERE medical_record_number = ?", (mrn,)).fetchone():
                seed(connection, now)
            phone = DEMO_PHONES.get(mrn)
            if phone and not connection.execute("SELECT 1 FROM patients WHERE phone = ?", (phone,)).fetchone():
                connection.execute("UPDATE patients SET phone = ? WHERE medical_record_number = ? AND phone IS NULL", (phone, mrn))
        _seed_ananya_rt(connection, now)

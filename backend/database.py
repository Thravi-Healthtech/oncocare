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
    "patients": [("phone", "TEXT")],
    "patient_reports": [("checkin_json", "TEXT"), ("ui_language", "TEXT")],
}

# Development clinician account (password comes from ONCOCARE_DEV_PASSWORD until OTP login is added).
DEV_CLINICIANS = [("Dr. R. Khanna", "8008475309", "Medical oncology")]
DEMO_PHONES = {"HN-24018": "9000000018", "HN-24031": "9000000031", "HN-24077": "9000000077"}


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


DEMO_PATIENTS = [("HN-24018", _seed_ananya), ("HN-24031", _seed_rahul), ("HN-24077", _seed_priya)]


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

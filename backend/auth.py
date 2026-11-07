"""Hospital sign-in (username + password) for doctors and staff, and the patient-portal dev password check.

Clinician accounts (radiodoc, chemodoc, surgerydoc, staff) each have their own salted PBKDF2 password hash;
see STAFF_ACCOUNTS in database.py. Patients still use the shared ONCOCARE_DEV_PASSWORD until OTP is added.
Sessions are random tokens stored hashed and sent as HttpOnly cookies.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import hash_password, row_or_none

SESSION_HOURS = 12
DOCTOR_COOKIE = "oncocare_doctor"


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def dev_password_ok(password: str | None) -> bool:
    expected = os.environ.get("ONCOCARE_DEV_PASSWORD", "1234")
    return hmac.compare_digest(str(password or ""), expected)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def clinician_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "username": row.get("username"), "name": row["name"], "phone": row["phone"],
            "specialty": row["specialty"], "role": row.get("role") or "doctor"}


def password_ok(row: dict[str, Any], password: str) -> bool:
    if not row.get("password_hash") or not row.get("password_salt"):
        return False
    return hmac.compare_digest(hash_password(str(password or ""), row["password_salt"])[0], row["password_hash"])


def login_clinician(connection: sqlite3.Connection, username: str, password: str) -> tuple[str, dict[str, Any]]:
    name = str(username or "").strip().lower()
    generic = AuthError(401, "Username or password is not correct.")
    clinician = row_or_none(connection, "SELECT * FROM clinicians WHERE username = ?", (name,)) if name else None
    if not clinician:
        raise generic
    if not clinician["active"]:
        raise AuthError(403, "This account is disabled.")
    now = _now()
    # No lockout after failed attempts (removed for the demo); failures are still counted.
    if not password_ok(clinician, password):
        connection.execute("UPDATE clinicians SET failed_attempts = failed_attempts + 1, locked_until = NULL WHERE id = ?", (clinician["id"],))
        connection.commit()
        raise generic
    token = secrets.token_urlsafe(32)
    connection.execute("UPDATE clinicians SET failed_attempts = 0, locked_until = NULL, last_login_at = ? WHERE id = ?", (now.isoformat(), clinician["id"]))
    connection.execute("DELETE FROM clinician_sessions WHERE expires_at < ?", (now.isoformat(),))
    connection.execute("INSERT INTO clinician_sessions (token_hash, clinician_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                       (token_hash(token), clinician["id"], now.isoformat(), (now + timedelta(hours=SESSION_HOURS)).isoformat()))
    return token, clinician_view(clinician)


def session_clinician(connection: sqlite3.Connection, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    row = row_or_none(connection, """SELECT c.*, s.expires_at FROM clinician_sessions s JOIN clinicians c ON c.id = s.clinician_id
                                     WHERE s.token_hash = ?""", (token_hash(token),))
    if not row or not row["active"] or datetime.fromisoformat(row["expires_at"]) < _now():
        return None
    return clinician_view(row)


def logout_clinician(connection: sqlite3.Connection, token: str | None) -> None:
    if token:
        connection.execute("DELETE FROM clinician_sessions WHERE token_hash = ?", (token_hash(token),))

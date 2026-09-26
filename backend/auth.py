"""Temporary phone + password sign-in for clinicians (and the shared dev password check).

Until OTP is integrated, every account uses the development password from ONCOCARE_DEV_PASSWORD
(default "1234"). Sessions are random tokens stored hashed and sent as HttpOnly cookies.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import normalise_phone, row_or_none

SESSION_HOURS = 12
MAX_FAILED = 5
LOCK_MINUTES = 15
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
    return {"id": row["id"], "name": row["name"], "phone": row["phone"], "specialty": row["specialty"]}


def login_clinician(connection: sqlite3.Connection, phone: str, password: str) -> tuple[str, dict[str, Any]]:
    number = normalise_phone(phone)
    generic = AuthError(401, "Phone number or password is not correct.")
    clinician = row_or_none(connection, "SELECT * FROM clinicians WHERE phone = ?", (number,)) if number else None
    if not clinician:
        raise generic
    if not clinician["active"]:
        raise AuthError(403, "This account is disabled.")
    now = _now()
    if clinician["locked_until"] and datetime.fromisoformat(clinician["locked_until"]) > now:
        raise AuthError(429, "Too many attempts. Try again in 15 minutes.")
    if not dev_password_ok(password):
        failed = clinician["failed_attempts"] + 1
        locked = (now + timedelta(minutes=LOCK_MINUTES)).isoformat() if failed >= MAX_FAILED else None
        connection.execute("UPDATE clinicians SET failed_attempts = ?, locked_until = ? WHERE id = ?", (0 if locked else failed, locked, clinician["id"]))
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

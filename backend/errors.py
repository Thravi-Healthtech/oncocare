"""Shared error type for API handlers and the feature modules they call."""
from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(self, status: int, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details or {}

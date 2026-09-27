"""Redaction on the write path.

Placed where data *leaves* the system -- into an artifact, a log line, an
evidence file -- rather than trusting each call site to remember. This is
regulated financial data; the safe default is that anything resembling a
secret or an identifier never lands on disk in the first place.

Redaction is deliberately conservative: it would rather mask a harmless value
than leak a real one. False positives cost a reviewer a moment of confusion;
false negatives are a compliance incident.
"""

from __future__ import annotations

import re
from typing import Any

PLACEHOLDER = "[REDACTED]"

#: Field names whose values never get written, whatever they contain.
SENSITIVE_FIELD_HINTS = (
    "password", "passwd", "secret", "token", "api_key", "apikey", "authorization",
    "auth", "cookie", "session_id", "ssn", "social_security", "tax_id", "ein",
    "account_number", "acct_no", "routing", "card", "cvv", "pin", "dob",
    "date_of_birth", "credential",
)

#: Value shapes that are sensitive wherever they appear, including inside free
#: text scraped off a screen.
VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}\b", re.I)),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9._\-]{16,}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
)


def is_sensitive_name(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in SENSITIVE_FIELD_HINTS)


def redact_text(text: str) -> str:
    """Mask sensitive value shapes inside free text."""
    if not text:
        return text
    for _, pattern in VALUE_PATTERNS:
        text = pattern.sub(PLACEHOLDER, text)
    return text


def redact_value(name: str, value: Any) -> Any:
    """Mask by field name first, then by value shape."""
    if is_sensitive_name(name):
        return PLACEHOLDER
    return redact_text(value) if isinstance(value, str) else value


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    return {k: redact_value(k, v) for k, v in data.items()}


def redact_deep(obj: Any, *, _key: str = "") -> Any:
    """Recursively redact a nested structure before it is serialized."""
    if isinstance(obj, dict):
        return {k: (PLACEHOLDER if is_sensitive_name(k) else redact_deep(v, _key=k))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_deep(v, _key=_key) for v in obj]
    if isinstance(obj, str):
        return PLACEHOLDER if is_sensitive_name(_key) else redact_text(obj)
    return obj

"""The one gate every LLM-generated SQL query must pass through before execution.

Every string that reaches this module is untrusted, regardless of whether it came
from the dashboard generator or the chatbot. See docs/BACKEND_DESIGN.md section 4.
"""

from __future__ import annotations

import re
import sqlite3
import threading

DEFAULT_ROW_LIMIT = 500
QUERY_TIMEOUT_SECONDS = 5.0

_BLOCKED_KEYWORDS = ("ATTACH", "DETACH", "PRAGMA", "VACUUM")

_LIMIT_RE = re.compile(r"\blimit\b", re.IGNORECASE)
_LEADING_COMMENT_RE = re.compile(r"^\s*(--[^\n]*\n|/\*.*?\*/\s*)*", re.DOTALL)


class GuardrailError(Exception):
    """Raised when a query fails a guardrail check. Message is safe to show the model."""


def _strip_leading_comments(sql: str) -> str:
    return _LEADING_COMMENT_RE.sub("", sql).strip()


def _validate(query: str) -> str:
    """Validate a query string and return the (possibly limit-appended) query to run."""
    if not query or not query.strip():
        raise GuardrailError("Query is empty.")

    stripped = query.strip()

    # Single statement only: no semicolon except a single trailing one.
    body = stripped[:-1] if stripped.endswith(";") else stripped
    if ";" in body:
        raise GuardrailError(
            "Only a single SQL statement is allowed — remove the extra ';'."
        )

    cleaned = _strip_leading_comments(body)
    if not cleaned:
        raise GuardrailError("Query is empty after removing comments.")

    first_token = re.match(r"[A-Za-z]+", cleaned)
    first_word = first_token.group(0).upper() if first_token else ""
    if first_word not in ("SELECT", "WITH"):
        raise GuardrailError(
            "Only SELECT statements are allowed (optionally starting with WITH)."
        )

    upper = cleaned.upper()
    for keyword in _BLOCKED_KEYWORDS:
        if re.search(rf"\b{keyword}\b", upper):
            raise GuardrailError(f"'{keyword}' is not allowed in queries.")

    if not _LIMIT_RE.search(cleaned):
        cleaned = f"{cleaned}\nLIMIT {DEFAULT_ROW_LIMIT}"

    return cleaned


def execute_safe(db_path: str, query: str) -> list[dict]:
    """Validate and run a read-only query against the session database.

    Raises GuardrailError for anything that fails validation, and sqlite3.Error /
    TimeoutError for execution problems (bad SQL, timeout) — callers (chatbot,
    dashboard) should catch both and feed the message back to the model so it can
    self-correct rather than surfacing a raw error to the user.
    """
    safe_query = _validate(query)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=QUERY_TIMEOUT_SECONDS)
    try:
        conn.row_factory = sqlite3.Row

        cancelled = threading.Event()

        def _progress_handler() -> int:
            return 1 if cancelled.is_set() else 0

        conn.set_progress_handler(_progress_handler, 1000)

        timer = threading.Timer(QUERY_TIMEOUT_SECONDS, cancelled.set)
        timer.start()
        try:
            cursor = conn.execute(safe_query)
            rows = cursor.fetchall()
        except sqlite3.OperationalError as exc:
            if cancelled.is_set():
                raise TimeoutError(
                    f"Query exceeded the {QUERY_TIMEOUT_SECONDS}s time limit."
                ) from exc
            raise
        finally:
            timer.cancel()

        return [dict(row) for row in rows]
    finally:
        conn.close()

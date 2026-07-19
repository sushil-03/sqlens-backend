"""Abuse protection. No auth exists in this app, so client IP and session_id
are the only things to key limits on. See BACKEND_DESIGN §4a.

session_id alone is NOT enough: it's handed out freely on every upload, so a
scripted client can trivially "reset" its session-scoped quota by just
uploading again to mint a fresh session_id. Every limit that actually protects
the account's budget is therefore keyed on client IP, with session_id limits
layered on top for the well-behaved (browser) case.

Single-process, in-memory only for the per-IP/per-session counters — fine for
a hackathon single-instance deploy. The one thing that isn't allowed to reset
on a restart is the global cost budget, which is persisted (see
session_store.GlobalUsage) since that's the actual "don't drain my account"
backstop.

All limits are environment-configurable so the operator can tune them for
their own budget comfort without touching code.
"""

from __future__ import annotations

import os
import threading
import time

from fastapi import HTTPException

from app.session_store import global_usage


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


CHAT_MAX_PER_SESSION = _env_int("SQLENS_CHAT_MAX_PER_SESSION", 30)
CHAT_MAX_PER_MINUTE = _env_int("SQLENS_CHAT_MAX_PER_MINUTE", 6)
CLAUDE_CALL_CEILING_PER_SESSION = _env_int("SQLENS_CLAUDE_CALLS_PER_SESSION", 50)

# Per-IP limits — the real backstop, since session_id is free to regenerate.
UPLOADS_MAX_PER_IP_PER_HOUR = _env_int("SQLENS_UPLOADS_PER_IP_PER_HOUR", 10)
UPLOADS_MAX_PER_IP_PER_DAY = _env_int("SQLENS_UPLOADS_PER_IP_PER_DAY", 30)
CLAUDE_CALLS_MAX_PER_IP_PER_DAY = _env_int("SQLENS_CLAUDE_CALLS_PER_IP_PER_DAY", 150)

# The actual "don't exhaust the account" ceiling: a lifetime USD budget across
# every session and every IP, persisted across restarts. Once hit, the whole
# demo stops making Claude calls until the operator raises it.
GLOBAL_BUDGET_USD = _env_float("SQLENS_GLOBAL_BUDGET_USD", 5.0)


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._chat_events: dict[str, list[float]] = {}
        self._upload_events: dict[str, list[float]] = {}
        self._ip_claude_calls: dict[str, list[float]] = {}

    def check_chat_allowed(self, session_id: str) -> None:
        now = time.time()
        with self._lock:
            events = self._chat_events.setdefault(session_id, [])
            events[:] = [t for t in events if now - t < 3600]  # keep last hour

            if len(events) >= CHAT_MAX_PER_SESSION:
                raise HTTPException(
                    status_code=429,
                    detail="You've reached this session's chat message limit. Start a new upload to continue.",
                )

            recent = [t for t in events if now - t < 60]
            if len(recent) >= CHAT_MAX_PER_MINUTE:
                raise HTTPException(
                    status_code=429,
                    detail="Slow down — too many messages in the last minute. Try again shortly.",
                )

            events.append(now)

    def check_upload_allowed(self, ip: str) -> None:
        """Cap how many sessions a single IP can create — the real defense
        against "just re-upload to reset your session quota"."""
        now = time.time()
        with self._lock:
            events = self._upload_events.setdefault(ip, [])
            events[:] = [t for t in events if now - t < 86400]  # keep last day

            if len(events) >= UPLOADS_MAX_PER_IP_PER_DAY:
                raise HTTPException(
                    status_code=429,
                    detail="Too many uploads from this connection today. Please try again tomorrow.",
                )

            recent = [t for t in events if now - t < 3600]
            if len(recent) >= UPLOADS_MAX_PER_IP_PER_HOUR:
                raise HTTPException(
                    status_code=429,
                    detail="Too many uploads from this connection in the last hour. Please slow down.",
                )

            events.append(now)

    def check_claude_call_allowed(
        self, session_id: str, claude_call_count: int, ip: str | None = None
    ) -> None:
        with self._lock:
            if claude_call_count >= CLAUDE_CALL_CEILING_PER_SESSION:
                raise HTTPException(
                    status_code=429,
                    detail="This session has hit its Claude API call limit. Start a new upload to continue.",
                )

            if ip is not None:
                now = time.time()
                events = self._ip_claude_calls.setdefault(ip, [])
                events[:] = [t for t in events if now - t < 86400]
                if len(events) >= CLAUDE_CALLS_MAX_PER_IP_PER_DAY:
                    raise HTTPException(
                        status_code=429,
                        detail="This connection has hit its daily AI usage limit. Please try again tomorrow.",
                    )

        if global_usage.cost_usd() >= GLOBAL_BUDGET_USD:
            raise HTTPException(
                status_code=503,
                detail="This demo has hit its usage budget for now. Please try again later.",
            )

    def record_claude_call(self, ip: str | None = None) -> None:
        if ip is None:
            return
        with self._lock:
            self._ip_claude_calls.setdefault(ip, []).append(time.time())


limiter = RateLimiter()

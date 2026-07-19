"""Session registry: in-memory for speed, with a JSON sidecar per session on
disk so a backend restart mid-demo doesn't wipe an uploaded dataset. Chat
history is deliberately NOT persisted (the SDK's tool_runner conversation
objects aren't reliably JSON-round-trippable) — a restart resumes the dataset,
schema, dashboard, and knowledge, but starts the visible chat fresh, which is
a far better failure mode than losing the session outright.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

from app.claude_client import estimate_cost_usd

logger = logging.getLogger(__name__)

SESSIONS_DIR = os.path.join(tempfile.gettempdir(), "sqlens_sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

_PERSISTED_FIELDS = (
    "session_id",
    "db_path",
    "table_sources",
    "schema_context",
    "extra_context",
    "dashboard",
    "claude_call_count",
    "input_tokens",
    "output_tokens",
    "created_at",
)


@dataclass
class SessionState:
    session_id: str
    db_path: str
    table_sources: dict[str, str] = field(default_factory=dict)  # table_name -> source filename
    schema_context: str | None = None  # base context derived from the schema only
    extra_context: str | None = None  # user-supplied business context / notes
    dashboard: dict | None = None
    chat_history: list[dict] = field(default_factory=list)
    claude_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    created_at: float = field(default_factory=time.time)

    def full_context(self) -> str:
        """Schema context plus any user-supplied knowledge, combined for prompts."""
        base = self.schema_context or ""
        if not self.extra_context:
            return base
        return (
            f"{base}\n\n"
            "Additional context provided by the user about this dataset — treat "
            "this as authoritative business knowledge, not just data:\n"
            f"{self.extra_context}"
        )

    def _sidecar_path(self) -> str:
        return os.path.join(SESSIONS_DIR, f"{self.session_id}.json")

    def save(self) -> None:
        """Persist everything except chat_history to a JSON sidecar. Best-effort
        — a failed write shouldn't break the request that triggered it."""
        payload = {k: v for k, v in asdict(self).items() if k in _PERSISTED_FIELDS}
        path = self._sidecar_path()
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(payload, f)
            os.replace(tmp_path, path)
        except OSError:
            logger.warning("Failed to persist session %s", self.session_id, exc_info=True)

    @classmethod
    def load(cls, path: str) -> "SessionState | None":
        try:
            with open(path) as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            logger.warning("Skipping unreadable session sidecar %s", path, exc_info=True)
            return None

        if not os.path.exists(payload.get("db_path", "")):
            # The session's SQLite file is gone (e.g. temp dir cleared) —
            # the sidecar is no longer usable, drop it rather than resurrect
            # a session with no data behind it.
            return None

        return cls(**{k: payload[k] for k in _PERSISTED_FIELDS if k in payload})


_GLOBAL_USAGE_PATH = os.path.join(SESSIONS_DIR, "_global_usage.json")


class GlobalUsage:
    """Process-wide token totals across all sessions — the operator's lifetime
    spend tracker. Persisted to disk: a hard budget cutoff is only meaningful
    if a restart can't reset the clock back to zero."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.input_tokens = 0
        self.output_tokens = 0
        self.claude_calls = 0
        self._load()

    def _load(self) -> None:
        try:
            with open(_GLOBAL_USAGE_PATH) as f:
                payload = json.load(f)
            self.input_tokens = payload.get("input_tokens", 0)
            self.output_tokens = payload.get("output_tokens", 0)
            self.claude_calls = payload.get("claude_calls", 0)
        except (OSError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        try:
            tmp_path = f"{_GLOBAL_USAGE_PATH}.tmp"
            with open(tmp_path, "w") as f:
                json.dump(
                    {
                        "input_tokens": self.input_tokens,
                        "output_tokens": self.output_tokens,
                        "claude_calls": self.claude_calls,
                    },
                    f,
                )
            os.replace(tmp_path, _GLOBAL_USAGE_PATH)
        except OSError:
            logger.warning("Failed to persist global usage", exc_info=True)

    def add(self, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.claude_calls += 1
            self._save()

    def reset(self) -> None:
        """Zero the counter — e.g. after rotating to a fresh API key/balance.
        On a host with no shell access (Render's free tier), this is the only
        way to clear it short of a redeploy wiping the ephemeral disk."""
        with self._lock:
            self.input_tokens = 0
            self.output_tokens = 0
            self.claude_calls = 0
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "claude_calls": self.claude_calls,
                "cost_usd": round(estimate_cost_usd(self.input_tokens, self.output_tokens), 4),
            }

    def cost_usd(self) -> float:
        with self._lock:
            return estimate_cost_usd(self.input_tokens, self.output_tokens)


global_usage = GlobalUsage()


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        for path in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
            state = SessionState.load(path)
            if state is not None:
                self._sessions[state.session_id] = state
        if self._sessions:
            logger.info("Restored %d session(s) from disk", len(self._sessions))

    def create(self, db_path: str) -> SessionState:
        session_id = uuid.uuid4().hex
        state = SessionState(session_id=session_id, db_path=db_path)
        with self._lock:
            self._sessions[session_id] = state
        state.save()
        return state

    def get(self, session_id: str) -> SessionState | None:
        with self._lock:
            return self._sessions.get(session_id)

    def require(self, session_id: str) -> SessionState:
        state = self.get(session_id)
        if state is None:
            raise KeyError(session_id)
        return state


store = SessionStore()

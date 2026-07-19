from __future__ import annotations

import asyncio
import os

from anyio import to_thread
from fastapi import (
    Form,
    FastAPI,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import chatbot, dashboard
from app.claude_client import ClaudeRequestError, ClaudeUnavailableError, estimate_cost_usd
from app.guardrails import GuardrailError, execute_safe
from app.rate_limit import limiter
from app.schema import build_schema_context, extract_schema, schema_to_dict
from app.session_store import SessionState, global_usage, store
from app.sql_ingest import IngestError, load_files

app = FastAPI(title="SQLens Backend")

# Comma-separated list of allowed frontend origins. Defaults cover local dev
# plus the deployed Vercel frontend; add any additional domain (custom domain,
# preview deployments, etc.) via SQLENS_ALLOWED_ORIGINS in the environment
# rather than editing this list.
_DEFAULT_ORIGINS = "http://localhost:3000,https://sqlens-frontend.vercel.app"
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("SQLENS_ALLOWED_ORIGINS", _DEFAULT_ORIGINS).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SAMPLE_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sample_data")

SAMPLE_DATASETS: dict[str, list[str]] = {
    "ecommerce": ["customers.sql", "orders.sql", "products.sql"],
}

MAX_CONTEXT_CHARS = 4000


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def get_session(session_id: str) -> SessionState:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session_id. Upload a file first.")
    return session


def _record_usage(session: SessionState, usage: dict) -> None:
    session.input_tokens += usage.get("input_tokens", 0)
    session.output_tokens += usage.get("output_tokens", 0)
    global_usage.add(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
    # Persists claude_call_count/token totals and (after a dashboard generation)
    # the dashboard itself — chat_history is excluded from the sidecar, so this
    # is cheap even called after every chat turn.
    session.save()


def _session_usage(session: SessionState) -> dict:
    return {
        "input_tokens": session.input_tokens,
        "output_tokens": session.output_tokens,
        "claude_calls": session.claude_call_count,
        "cost_usd": round(estimate_cost_usd(session.input_tokens, session.output_tokens), 4),
    }


def _run_claude_call(fn):
    """Translate the app's Claude error types into clean HTTP responses instead
    of letting an unhandled exception surface as a raw 500."""
    try:
        return fn()
    except ClaudeUnavailableError:
        raise HTTPException(
            status_code=502,
            detail="Claude is temporarily unavailable — please try again in a moment.",
        )
    except ClaudeRequestError:
        raise HTTPException(
            status_code=502,
            detail="Something about that request Claude couldn't process — please try again.",
        )


def _clean_context(context: str | None) -> str | None:
    if context is None:
        return None
    trimmed = context.strip()
    if not trimmed:
        return None
    if len(trimmed) > MAX_CONTEXT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Context is too long: max {MAX_CONTEXT_CHARS} characters.",
        )
    return trimmed


def _ingest_and_create_session(files: list[tuple[str, bytes]], context: str | None = None) -> dict:
    try:
        db_path, table_sources, results = load_files(files)
    except IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    session = store.create(db_path)
    session.table_sources = table_sources
    session.extra_context = _clean_context(context)

    schema = extract_schema(db_path, table_sources)
    session.schema_context = build_schema_context(schema)
    session.save()

    return {
        "session_id": session.session_id,
        "files": [
            {"filename": r.filename, "ok": r.ok, "tables_added": r.tables_added, "error": r.error}
            for r in results
        ],
        "schema": schema_to_dict(schema),
    }


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/upload")
async def upload(
    request: Request, files: list[UploadFile], context: str | None = Form(None)
) -> dict:
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")
    # session_id is minted fresh on every upload, so per-session limits alone
    # can be bypassed by just re-uploading — this is the real per-IP backstop.
    limiter.check_upload_allowed(_client_ip(request))
    contents = [(f.filename or "upload.sql", await f.read()) for f in files]
    # _ingest_and_create_session does CPU-bound parsing (SQL execution, pandas
    # read_csv/read_excel) that can take tens of seconds for a large file —
    # run it in a worker thread so it can't block the event loop (and every
    # other session's WebSocket chat) for the duration.
    return await to_thread.run_sync(_ingest_and_create_session, contents, context)


@app.post("/api/samples/{name}")
def load_sample(request: Request, name: str, context: str | None = None) -> dict:
    if name not in SAMPLE_DATASETS:
        raise HTTPException(status_code=404, detail=f"Unknown sample dataset '{name}'.")

    limiter.check_upload_allowed(_client_ip(request))

    contents: list[tuple[str, bytes]] = []
    for filename in SAMPLE_DATASETS[name]:
        path = os.path.join(SAMPLE_DATA_DIR, name, filename)
        with open(path, "rb") as f:
            contents.append((filename, f.read()))

    return _ingest_and_create_session(contents, context)


class ContextRequest(BaseModel):
    context: str


@app.put("/api/context/{session_id}")
def update_context(session_id: str, body: ContextRequest) -> dict:
    """Add or replace the user-supplied business context for a session. Clears
    the cached dashboard so the next fetch regenerates it with the new
    knowledge in scope."""
    session = get_session(session_id)
    session.extra_context = _clean_context(body.context)
    session.dashboard = None
    session.save()
    return {"extra_context": session.extra_context}


class QueryRequest(BaseModel):
    sql: str


@app.post("/api/query/{session_id}")
def run_query(session_id: str, body: QueryRequest) -> dict:
    """Re-run an already-known SQL string (e.g. a dashboard chart's query, to
    refresh its data) against the session's database. Goes through the same
    guardrails as any other query — no LLM call, so no Claude-call-ceiling or
    token cost, but still counts against the per-session request limiter since
    it's still session activity worth capping."""
    session = get_session(session_id)
    limiter.check_chat_allowed(session_id)
    try:
        data = execute_safe(session.db_path, body.sql)
    except GuardrailError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Query failed: {exc}")
    return {"data": data}


@app.get("/api/schema/{session_id}")
def get_schema(session_id: str) -> dict:
    session = get_session(session_id)
    schema = extract_schema(session.db_path, session.table_sources)
    return schema_to_dict(schema)


@app.get("/api/dashboard/{session_id}")
def get_dashboard(request: Request, session_id: str) -> dict:
    session = get_session(session_id)

    if session.dashboard is not None:
        return session.dashboard

    ip = _client_ip(request)
    limiter.check_claude_call_allowed(session_id, session.claude_call_count, ip)
    result = _run_claude_call(
        lambda: dashboard.generate_dashboard(session.db_path, session.full_context())
    )
    limiter.record_claude_call(ip)
    session.claude_call_count += 1
    usage = result.pop("usage")
    session.dashboard = result
    _record_usage(session, usage)

    return result


@app.get("/api/usage/{session_id}")
def get_usage(session_id: str) -> dict:
    session = get_session(session_id)
    return {"session": _session_usage(session), "global": global_usage.snapshot()}


# Admin endpoints for the global spend counter — not tied to any session, so
# they need their own auth. Disabled entirely unless SQLENS_ADMIN_KEY is set;
# on a host with no shell/disk access (Render's free tier), this is the only
# way to inspect or reset the counter without a full redeploy.
ADMIN_KEY = os.environ.get("SQLENS_ADMIN_KEY")


def _require_admin(key: str | None) -> None:
    if not ADMIN_KEY:
        raise HTTPException(status_code=404, detail="Not found.")
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key.")


@app.get("/api/admin/usage")
def admin_get_usage(key: str | None = None) -> dict:
    _require_admin(key)
    return global_usage.snapshot()


@app.post("/api/admin/usage/reset")
def admin_reset_usage(key: str | None = None) -> dict:
    _require_admin(key)
    global_usage.reset()
    return global_usage.snapshot()


class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat/{session_id}")
def chat(request: Request, session_id: str, body: ChatRequest) -> dict:
    session = get_session(session_id)
    ip = _client_ip(request)

    limiter.check_chat_allowed(session_id)
    limiter.check_claude_call_allowed(session_id, session.claude_call_count, ip)

    result = _run_claude_call(
        lambda: chatbot.ask(
            session.db_path, session.full_context(), session.chat_history, body.message
        )
    )
    limiter.record_claude_call(ip)
    session.claude_call_count += 1
    session.chat_history = result["updated_history"]
    _record_usage(session, result["usage"])

    return {
        "reply": result["reply"],
        "sql_used": result["sql_used"],
        "charts": result["charts"],
        "usage": _session_usage(session),
    }


@app.websocket("/ws/chat/{session_id}")
async def chat_ws(websocket: WebSocket, session_id: str) -> None:
    """Streaming chat: client sends {"message": str}, server streams events:
    {"type": "sql"|"chart"|"result"|"error"} and finally {"type": "done"}.
    """
    await websocket.accept()
    ip = websocket.client.host if websocket.client else "unknown"

    session = store.get(session_id)
    if session is None:
        await websocket.send_json(
            {"type": "error", "detail": "Unknown session_id. Upload a file first."}
        )
        await websocket.close()
        return

    try:
        while True:
            data = await websocket.receive_json()
            message = str(data.get("message", "")).strip()
            if not message:
                await websocket.send_json({"type": "error", "detail": "Empty message."})
                continue

            try:
                limiter.check_chat_allowed(session_id)
                limiter.check_claude_call_allowed(session_id, session.claude_call_count, ip)
            except HTTPException as exc:
                await websocket.send_json({"type": "error", "detail": exc.detail})
                await websocket.send_json({"type": "done"})
                continue

            loop = asyncio.get_running_loop()
            events: asyncio.Queue[dict] = asyncio.Queue()

            def emit(event: dict) -> None:
                loop.call_soon_threadsafe(events.put_nowait, event)

            def work(user_message: str = message) -> dict | None:
                try:
                    result = chatbot.ask(
                        session.db_path,
                        session.full_context(),
                        session.chat_history,
                        user_message,
                        on_event=emit,
                    )
                    emit(
                        {
                            "type": "result",
                            "reply": result["reply"],
                            "sql_used": result["sql_used"],
                        }
                    )
                    return result
                except ClaudeUnavailableError:
                    emit(
                        {
                            "type": "error",
                            "detail": "Claude is temporarily unavailable — please try again in a moment.",
                        }
                    )
                    return None
                except ClaudeRequestError:
                    emit(
                        {
                            "type": "error",
                            "detail": "Something about that request Claude couldn't process — please try again.",
                        }
                    )
                    return None
                except Exception:
                    emit(
                        {
                            "type": "error",
                            "detail": "Something went wrong answering that — please try again.",
                        }
                    )
                    return None

            task = loop.run_in_executor(None, work)

            while True:
                event = await events.get()
                await websocket.send_json(event)
                if event["type"] in ("result", "error"):
                    break

            result = await task
            if result is not None:
                limiter.record_claude_call(ip)
                session.claude_call_count += 1
                session.chat_history = result["updated_history"]
                _record_usage(session, result["usage"])

            await websocket.send_json({"type": "done", "usage": _session_usage(session)})
    except WebSocketDisconnect:
        pass

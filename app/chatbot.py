"""Text-to-SQL chatbot via the Anthropic Tool Runner. See BACKEND_DESIGN §5b.

One tool: run_sql_query (answer questions with data). Chat deliberately only
ever replies in text — no chart rendering here, that's what the dashboard is
for. Progress events (each SQL run) are pushed through an optional on_event
callback so the WebSocket endpoint can stream them to the frontend as they
happen.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Callable

from anthropic import beta_tool

from app.claude_client import CHAT_MODEL, call_with_retry, get_client
from app.guardrails import GuardrailError, execute_safe

CHAT_SYSTEM_PROMPT_TEMPLATE = (
    "You answer questions about a database using the run_sql_query tool. "
    "Always write a single SELECT statement (optionally starting with WITH), "
    "never use a semicolon, and always include a LIMIT. Use the relationships "
    "section to join across tables correctly. If a query fails, read the error "
    "and try a corrected query rather than giving up.\n\n"
    "Always answer in clear, well-formatted text — never mention or ask about "
    "charts or visualizations, since none are available here. Summarize "
    "results directly: for a breakdown or ranking, use a short markdown list "
    "or table rather than a wall of prose.\n\n"
    "Be direct and concise — lead with the answer, not a restatement of the "
    "question.\n\n"
    "{schema_context}"
)

EventCallback = Callable[[dict], None]


def _run_query(db_path: str, query: str) -> tuple[list[dict] | None, str | None]:
    try:
        return execute_safe(db_path, query), None
    except GuardrailError as exc:
        return None, str(exc)
    except sqlite3.Error as exc:
        return None, f"SQL error: {exc}"
    except TimeoutError as exc:
        return None, str(exc)


def _make_tools(db_path: str, sql_log: list[str], emit: EventCallback):
    @beta_tool
    def run_sql_query(query: str) -> str:
        """Run a read-only SQL query against the uploaded dataset and return the
        results as JSON. Only SELECT statements are allowed.

        Args:
            query: A single SELECT statement. Always include a LIMIT.
        """
        rows, error = _run_query(db_path, query)
        if error is not None:
            return json.dumps({"error": error})
        sql_log.append(query)
        emit({"type": "sql", "sql": query})
        return json.dumps(rows, default=str)

    return [run_sql_query]


def ask(
    db_path: str,
    schema_context: str,
    chat_history: list[dict],
    user_message: str,
    on_event: EventCallback | None = None,
) -> dict:
    """Run one turn of the chat loop. Returns {reply, sql_used, charts, updated_history}."""
    client = get_client()
    emit: EventCallback = on_event or (lambda event: None)
    messages = chat_history + [{"role": "user", "content": user_message}]

    # sql_log is captured by closure and reset on each attempt below so a
    # retry (after a transient failure mid-loop) doesn't leave duplicate
    # entries from the aborted attempt mixed into the result.
    state: dict = {}

    def _run_loop():
        sql_log: list[str] = []
        tools = _make_tools(db_path, sql_log, emit)

        runner = client.beta.messages.tool_runner(
            model=CHAT_MODEL,
            max_tokens=2048,
            system=CHAT_SYSTEM_PROMPT_TEMPLATE.format(schema_context=schema_context),
            tools=tools,
            messages=messages,
        )

        final_message = None
        input_tokens = 0
        output_tokens = 0
        for message in runner:
            final_message = message
            usage = getattr(message, "usage", None)
            if usage is not None:
                input_tokens += getattr(usage, "input_tokens", 0) or 0
                output_tokens += getattr(usage, "output_tokens", 0) or 0

        state["sql_log"] = sql_log
        state["final_message"] = final_message
        state["input_tokens"] = input_tokens
        state["output_tokens"] = output_tokens
        # No public accessor for the full conversation (incl. intermediate
        # tool_use / tool_result turns) in this SDK version — _params is the
        # runner's own cursor, already updated in place after the loop above.
        state["updated_history"] = list(runner._params["messages"])

    call_with_retry(_run_loop)

    reply_text = ""
    final_message = state["final_message"]
    if final_message is not None:
        for block in final_message.content:
            if getattr(block, "type", None) == "text":
                reply_text += block.text

    return {
        "reply": reply_text,
        "sql_used": state["sql_log"],
        "charts": [],
        "updated_history": state["updated_history"],
        "usage": {
            "input_tokens": state["input_tokens"],
            "output_tokens": state["output_tokens"],
        },
    }

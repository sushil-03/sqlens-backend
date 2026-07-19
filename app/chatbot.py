"""Text-to-SQL chatbot via the Anthropic Tool Runner. See BACKEND_DESIGN §5b.

Two tools: run_sql_query (answer questions with data) and show_chart (render a
visualization in the chat UI). Progress events — each SQL run and each chart —
are pushed through an optional on_event callback so the WebSocket endpoint can
stream them to the frontend as they happen.
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
    "When a visualization would communicate the answer better than prose — "
    "trends, comparisons, breakdowns, top-N rankings — call the show_chart tool "
    "to render a chart for the user, then summarize the key takeaway briefly in "
    "text. Don't repeat the chart's data point-by-point in prose. Don't chart a "
    "comparison with fewer than 3 categories — just say the numbers in text "
    "instead. If a breakdown has many small values, group the long tail into "
    "an 'Other' bucket rather than charting dozens of slivers.\n\n"
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


def _make_tools(db_path: str, sql_log: list[str], chart_log: list[dict], emit: EventCallback):
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

    @beta_tool
    def show_chart(title: str, chart_type: str, sql: str, x_field: str, y_fields: str) -> str:
        """Render a chart in the chat for the user. Use this when a visualization
        answers the question better than text (trends, comparisons, breakdowns,
        funnels, correlations) — and pick the type deliberately rather than
        defaulting to bar every time.

        Args:
            title: Short human-readable chart title.
            chart_type: One of: bar (category comparison), line/area (trend over
                time), pie (part-to-whole, <=6 categories), funnel (ordered stage
                progression, rows sorted largest to smallest), scatter
                (relationship between two numeric columns).
            sql: A single SELECT statement that produces the chart data. Always include a LIMIT.
            x_field: Result column to use for the x axis / category labels.
            y_fields: Comma-separated numeric result column(s) to plot.
        """
        allowed_types = ("bar", "line", "area", "pie", "funnel", "scatter")
        if chart_type not in allowed_types:
            return json.dumps({"error": f"chart_type must be one of: {', '.join(allowed_types)}"})

        rows, error = _run_query(db_path, sql)
        if error is not None:
            return json.dumps({"error": error})
        if not rows:
            return json.dumps({"error": "The query returned no rows — nothing to chart."})

        sql_log.append(sql)
        chart = {
            "title": title,
            "chart_type": chart_type,
            "sql": sql,
            "x_field": x_field,
            "y_fields": [f.strip() for f in y_fields.split(",") if f.strip()],
            "data": rows,
        }
        chart_log.append(chart)
        emit({"type": "chart", "chart": chart})
        return json.dumps(
            {"status": "Chart rendered and shown to the user.", "rows_plotted": len(rows)}
        )

    return [run_sql_query, show_chart]


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

    # sql_log/chart_log are captured by closure and reset on each attempt below
    # so a retry (after a transient failure mid-loop) doesn't leave duplicate
    # entries from the aborted attempt mixed into the result.
    state: dict = {}

    def _run_loop():
        sql_log: list[str] = []
        chart_log: list[dict] = []
        tools = _make_tools(db_path, sql_log, chart_log, emit)

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
        state["chart_log"] = chart_log
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
        "charts": state["chart_log"],
        "updated_history": state["updated_history"],
        "usage": {
            "input_tokens": state["input_tokens"],
            "output_tokens": state["output_tokens"],
        },
    }

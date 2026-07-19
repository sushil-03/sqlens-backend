"""Dashboard chart-spec generation via structured outputs. See BACKEND_DESIGN §5a."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

import sqlite3

from app.claude_client import DASHBOARD_MODEL, call_with_retry, get_client
from app.guardrails import GuardrailError, execute_safe

DASHBOARD_SYSTEM_PROMPT = (
    "You are a sharp, business-minded data analyst. Given a database schema "
    "(tables, columns, sample rows, and relationships between tables), propose "
    "5-7 useful dashboard charts that would give someone a quick, genuinely "
    "useful overview of this dataset. If the user has supplied additional "
    "business context, treat it as authoritative and let it steer which charts "
    "matter most.\n\n"
    "Rules for every chart:\n"
    "- Write exactly one read-only SELECT statement (optionally starting with WITH).\n"
    "- Never use a semicolon.\n"
    "- Always include a LIMIT clause.\n"
    "- Use the declared and inferred relationships to join across tables when useful.\n"
    "- Never propose a bar or pie chart with fewer than 3 categories in the "
    "result — a 1-2 category comparison is a stat or a short sentence, not a "
    "chart; it just wastes space. Use a stat instead.\n"
    "- If a categorical breakdown has more than ~8 distinct values, aggregate "
    "the long tail into a single 'Other' row (SUM/COUNT the rest) rather than "
    "returning dozens of tiny slices/bars nobody can read.\n\n"
    "Choose chart_type deliberately per chart — don't default everything to bar:\n"
    "- stat: a single headline number (a total, a rate, a count).\n"
    "- line / area: a metric over time (dates/months/sequential periods).\n"
    "- bar: comparing a metric across 3+ discrete categories.\n"
    "- pie: a part-to-whole breakdown with 3-6 categories.\n"
    "- funnel: an ordered progression through stages where each stage is a "
    "subset of the previous one (e.g. signup -> activated -> paid, or any "
    "status/pipeline field with a natural order) — order the rows from "
    "largest/first stage to smallest/last stage.\n"
    "- scatter: the relationship between two numeric measures across "
    "individual records (correlation, outliers).\n"
    "Across the charts you propose, use at least 3 distinct chart_type values, "
    "and prefer funnel or scatter when the schema naturally supports one — "
    "don't force them where they don't fit.\n\n"
    "Also write a `key_insight`: one or two sentences of the single most useful "
    "or surprising thing you notice in this data (a concentration, an anomaly, "
    "a dominant category, a trend) — something a busy person would want to "
    "know first, not a restatement of the schema."
)


class ChartSpec(BaseModel):
    title: str
    chart_type: Literal["bar", "line", "pie", "area", "stat", "funnel", "scatter"]
    sql: str
    x_field: str | None = None
    y_fields: list[str]


class DashboardSpec(BaseModel):
    key_insight: str
    charts: list[ChartSpec]


def generate_dashboard(db_path: str, schema_context: str) -> dict:
    """Ask Claude for a dashboard spec, run each chart's SQL, return {spec, data} pairs.

    Charts whose SQL fails guardrails or execution are dropped rather than
    failing the whole dashboard — a partial dashboard beats a blank one.
    """
    client = get_client()
    response = call_with_retry(
        lambda: client.messages.parse(
            model=DASHBOARD_MODEL,
            max_tokens=4096,
            system=DASHBOARD_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": schema_context}],
            output_format=DashboardSpec,
        )
    )
    spec = response.parsed_output

    charts: list[dict] = []
    for chart in spec.charts:
        try:
            data = execute_safe(db_path, chart.sql)
        except (GuardrailError, sqlite3.Error, TimeoutError):
            continue
        charts.append(
            {
                "title": chart.title,
                "chart_type": chart.chart_type,
                "sql": chart.sql,
                "x_field": chart.x_field,
                "y_fields": chart.y_fields,
                "data": data,
            }
        )

    usage = getattr(response, "usage", None)
    return {
        "key_insight": spec.key_insight,
        "charts": charts,
        "usage": {
            "input_tokens": (getattr(usage, "input_tokens", 0) or 0) if usage else 0,
            "output_tokens": (getattr(usage, "output_tokens", 0) or 0) if usage else 0,
        },
    }

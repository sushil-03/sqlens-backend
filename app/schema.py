"""Schema + relationship extraction, and the compact schema context string sent
to Claude on every dashboard-generation and chat request. See BACKEND_DESIGN §3, §3a.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

SAMPLE_ROWS_PER_TABLE = 3


@dataclass
class ColumnInfo:
    name: str
    type: str
    is_primary_key: bool


@dataclass
class Relationship:
    from_table: str
    from_column: str
    to_table: str
    to_column: str


@dataclass
class TableInfo:
    name: str
    source_file: str
    create_sql: str
    columns: list[ColumnInfo]
    row_count: int
    sample_rows: list[dict]


@dataclass
class SchemaInfo:
    tables: list[TableInfo]
    relationships: list[Relationship]


def _columns_for(conn: sqlite3.Connection, table: str) -> list[ColumnInfo]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [ColumnInfo(name=r[1], type=r[2] or "TEXT", is_primary_key=bool(r[5])) for r in rows]


def _relationships_for(conn: sqlite3.Connection, table: str) -> list[Relationship]:
    rows = conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    # PRAGMA foreign_key_list columns: id, seq, table, from, to, on_update, on_delete, match
    return [
        Relationship(from_table=table, from_column=r[3], to_table=r[2], to_column=r[4])
        for r in rows
    ]


def extract_schema(db_path: str, table_sources: dict[str, str]) -> SchemaInfo:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables: list[TableInfo] = []
        relationships: list[Relationship] = []

        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()

        for row in rows:
            table_name = row["name"]
            create_sql = row["sql"] or ""
            columns = _columns_for(conn, table_name)
            relationships.extend(_relationships_for(conn, table_name))

            row_count = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
            sample_rows = [
                dict(r)
                for r in conn.execute(
                    f'SELECT * FROM "{table_name}" LIMIT {SAMPLE_ROWS_PER_TABLE}'
                ).fetchall()
            ]

            tables.append(
                TableInfo(
                    name=table_name,
                    source_file=table_sources.get(table_name, "unknown"),
                    create_sql=create_sql,
                    columns=columns,
                    row_count=row_count,
                    sample_rows=sample_rows,
                )
            )

        return SchemaInfo(tables=tables, relationships=relationships)
    finally:
        conn.close()


def build_schema_context(schema: SchemaInfo) -> str:
    """Render the schema into a compact text block for the Claude system prompt."""
    parts: list[str] = []

    for table in schema.tables:
        parts.append(f"-- Table: {table.name} (from {table.source_file}, {table.row_count} rows)")
        parts.append(table.create_sql.strip() + ";")
        if table.sample_rows:
            parts.append(f"Sample rows from {table.name}:")
            for r in table.sample_rows:
                parts.append(f"  {r}")
        parts.append("")

    if schema.relationships:
        parts.append("Relationships:")
        for rel in schema.relationships:
            parts.append(f"  {rel.from_table}.{rel.from_column} -> {rel.to_table}.{rel.to_column}")
    else:
        parts.append(
            "Relationships: none declared explicitly. Infer joins from column naming "
            "conventions (e.g. a column named <table>_id likely references that table's "
            "primary key) and matching sample values across tables."
        )

    return "\n".join(parts)


def schema_to_dict(schema: SchemaInfo) -> dict:
    return {
        "tables": [
            {
                "name": t.name,
                "source_file": t.source_file,
                "row_count": t.row_count,
                "columns": [
                    {"name": c.name, "type": c.type, "is_primary_key": c.is_primary_key}
                    for c in t.columns
                ],
                "sample_rows": t.sample_rows,
            }
            for t in schema.tables
        ],
        "relationships": [
            {
                "from_table": r.from_table,
                "from_column": r.from_column,
                "to_table": r.to_table,
                "to_column": r.to_column,
            }
            for r in schema.relationships
        ],
    }

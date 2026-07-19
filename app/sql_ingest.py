"""Load one or more uploaded .sql/.csv/.xlsx/.xls files into a single shared
session SQLite DB.

See docs/BACKEND_DESIGN.md section 3. Load order doesn't matter for correctness
(SQLite doesn't enforce FOREIGN KEY constraints at CREATE TABLE / INSERT time
unless PRAGMA foreign_keys=ON, which we never set for ingestion), so files are
processed in whatever order they arrive.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, field
from io import BytesIO

MAX_FILES_PER_UPLOAD = 10
MAX_TOTAL_UPLOAD_BYTES = 100 * 1024 * 1024  # 100MB — CSV/Excel datasets run bigger than .sql dumps
MAX_ROWS_PER_TABULAR_FILE = 200_000  # sanity cap for CSV/Excel imports

SUPPORTED_EXTENSIONS = (".sql", ".csv", ".xlsx", ".xls")

SESSIONS_DIR = os.path.join(tempfile.gettempdir(), "sqlens_sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)


class IngestError(Exception):
    """Raised for upload-level problems (too many files, too large, bad SQL)."""


@dataclass
class FileResult:
    filename: str
    ok: bool
    tables_added: list[str] = field(default_factory=list)
    error: str | None = None


def new_db_path() -> str:
    return os.path.join(SESSIONS_DIR, f"{uuid.uuid4().hex}.sqlite3")


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def _tables_declared_in(sql_text: str) -> list[str]:
    """Best-effort scan for CREATE TABLE names in a SQL script, used only to
    attribute collisions to a filename before execution. Not a full parser —
    doesn't need to be, since executescript() is the real source of truth for
    whether the SQL is valid."""
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?(\w+)[\"'`\]]?",
        re.IGNORECASE,
    )
    return pattern.findall(sql_text)


def _format_bytes(n: int) -> str:
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _sanitize_table_name(name: str) -> str:
    cleaned = re.sub(r"\W+", "_", name).strip("_").lower()
    if not cleaned:
        cleaned = "table"
    if cleaned[0].isdigit():
        cleaned = f"t_{cleaned}"
    return cleaned


# sqlglot emits valid-but-not-quite-SQLite output for some MySQL constructs;
# these fixups run on each transpiled statement before execution.
_INT_SIZED_RE = re.compile(r"\b(INTEGER|INT)\(\d+\)", re.IGNORECASE)
_INLINE_INDEX_RE = re.compile(
    r",\s*(?:UNIQUE\s+)?INDEX\s+(?:\"[^\"]+\"|\S+)\s*\([^)]*\)", re.IGNORECASE
)


def _fix_sqlite_statement(sql: str) -> str:
    sql = _INT_SIZED_RE.sub(r"\1", sql)  # INTEGER(11) -> INTEGER (breaks AUTOINCREMENT otherwise)
    sql = _INLINE_INDEX_RE.sub("", sql)  # SQLite has no inline INDEX in CREATE TABLE
    return sql


def _load_via_transpile(conn: sqlite3.Connection, sql_text: str, dialect: str) -> tuple[bool, str | None]:
    """Parse with an explicit source dialect and execute statement by statement,
    skipping statements that can't run in SQLite (SET, LOCK TABLES, CREATE INDEX
    on missing columns, etc.). Succeeds if at least one statement executes.
    """
    try:
        import sqlglot

        expressions = sqlglot.parse(sql_text, read=dialect)
    except Exception as exc:
        return False, f"could not parse as {dialect}: {exc}"

    executed = 0
    last_error: str | None = None
    for expression in expressions:
        if expression is None:
            continue
        try:
            statement = _fix_sqlite_statement(expression.sql(dialect="sqlite"))
            conn.execute(statement)
            executed += 1
        except Exception as exc:
            last_error = str(exc)
            continue

    if executed == 0:
        return False, last_error or f"no statement from the {dialect} parse could run"
    return True, None


def _load_sql_file(conn: sqlite3.Connection, sql_text: str, before: set[str]) -> tuple[bool, list[str]]:
    """Try native SQLite, then MySQL/Postgres transpile fallbacks. Returns
    (success, error_messages)."""
    errors: list[str] = []

    def _drop_partial_tables() -> None:
        # executescript()/per-statement execution can leave partial tables
        # behind after a mid-script failure — clean up before the next
        # attempt so a failed attempt never leaks state.
        for table in _existing_tables(conn) - before:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.commit()

    try:
        conn.executescript(sql_text)
        return True, errors
    except sqlite3.Error as exc:
        errors.append(f"sqlite: {exc}")
        _drop_partial_tables()

    for dialect in ("mysql", "postgres"):
        ok, error = _load_via_transpile(conn, sql_text, dialect)
        if ok:
            return True, errors
        errors.append(f"{dialect}: {error}")
        _drop_partial_tables()

    return False, errors


def _dataframe_to_table(conn: sqlite3.Connection, df, table_name: str) -> None:
    if len(df) > MAX_ROWS_PER_TABULAR_FILE:
        df = df.iloc[:MAX_ROWS_PER_TABULAR_FILE]

    # Normalize column names: SQL-safe, unique, no leading digits.
    seen: dict[str, int] = {}
    columns = []
    for col in df.columns:
        base = _sanitize_table_name(str(col)) or "column"
        count = seen.get(base, 0)
        seen[base] = count + 1
        columns.append(base if count == 0 else f"{base}_{count}")
    df.columns = columns

    df = df.convert_dtypes()
    df.to_sql(table_name, conn, if_exists="fail", index=False)


def _load_csv_file(conn: sqlite3.Connection, content: bytes, table_name: str) -> None:
    import pandas as pd

    df = pd.read_csv(BytesIO(content))
    _dataframe_to_table(conn, df, table_name)


def _load_excel_file(
    conn: sqlite3.Connection, content: bytes, stem: str, before: set[str]
) -> list[str]:
    """Load every sheet as its own table. Single-sheet workbooks use the
    filename stem as the table name; multi-sheet workbooks use
    <stem>_<sheet>. Returns the list of new table names."""
    import pandas as pd

    sheets = pd.read_excel(BytesIO(content), sheet_name=None)
    new_tables: list[str] = []

    sheet_items = list(sheets.items())
    for sheet_name, df in sheet_items:
        table_name = _sanitize_table_name(stem)
        if len(sheet_items) > 1:
            table_name = f"{table_name}_{_sanitize_table_name(str(sheet_name))}"

        candidate = table_name
        suffix = 2
        while candidate in before or candidate in new_tables:
            candidate = f"{table_name}_{suffix}"
            suffix += 1

        _dataframe_to_table(conn, df, candidate)
        new_tables.append(candidate)

    return new_tables


def load_files(files: list[tuple[str, bytes]]) -> tuple[str, dict[str, str], list[FileResult]]:
    """Load (filename, content_bytes) pairs into one new shared SQLite DB.

    Supports .sql (executed as SQL, with MySQL/Postgres dialect fallback),
    and .csv/.xlsx/.xls (loaded as a table via pandas — one table per file,
    or one table per sheet for multi-sheet Excel workbooks).

    Returns (db_path, table_sources, per_file_results). table_sources maps
    table_name -> filename for tables that loaded successfully. Raises
    IngestError for upload-level limit violations.
    """
    if not files:
        raise IngestError("No files provided.")
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise IngestError(f"Too many files: max {MAX_FILES_PER_UPLOAD} per upload.")

    total_bytes = sum(len(content) for _, content in files)
    if total_bytes > MAX_TOTAL_UPLOAD_BYTES:
        raise IngestError(
            f"Upload too large: {_format_bytes(total_bytes)} exceeds the "
            f"{_format_bytes(MAX_TOTAL_UPLOAD_BYTES)} limit."
        )

    db_path = new_db_path()
    conn = sqlite3.connect(db_path)
    table_sources: dict[str, str] = {}
    results: list[FileResult] = []

    try:
        for filename, content in files:
            ext = os.path.splitext(filename)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                results.append(
                    FileResult(
                        filename,
                        ok=False,
                        error=f"Unsupported file type '{ext}'. Supported: {', '.join(SUPPORTED_EXTENSIONS)}.",
                    )
                )
                continue

            existing = _existing_tables(conn)

            if ext == ".sql":
                try:
                    sql_text = content.decode("utf-8", errors="replace")
                except Exception as exc:
                    results.append(FileResult(filename, ok=False, error=f"Could not decode file: {exc}"))
                    continue

                declared = _tables_declared_in(sql_text)
                collisions = [t for t in declared if t in existing]
                if collisions:
                    collided_with = [
                        f'"{t}" is also defined in {table_sources.get(t, "an earlier file")}'
                        for t in collisions
                    ]
                    results.append(
                        FileResult(
                            filename,
                            ok=False,
                            error=f"Table name collision in {filename}: " + "; ".join(collided_with),
                        )
                    )
                    continue

                success, errors = _load_sql_file(conn, sql_text, existing)
                if not success:
                    results.append(
                        FileResult(filename, ok=False, error=f"Failed to load {filename} — " + " | ".join(errors))
                    )
                    continue

                new_tables = sorted(_existing_tables(conn) - existing)
                for table in new_tables:
                    table_sources[table] = filename
                results.append(FileResult(filename, ok=True, tables_added=new_tables))
                continue

            # CSV / Excel: one table per file (or per sheet for Excel).
            stem = os.path.splitext(os.path.basename(filename))[0]
            try:
                if ext == ".csv":
                    table_name = _sanitize_table_name(stem)
                    if table_name in existing:
                        results.append(
                            FileResult(
                                filename,
                                ok=False,
                                error=(
                                    f'Table name collision: "{table_name}" (from {filename}) is '
                                    f'also defined in {table_sources.get(table_name, "an earlier file")}'
                                ),
                            )
                        )
                        continue
                    _load_csv_file(conn, content, table_name)
                    new_tables = [table_name]
                else:
                    new_tables = _load_excel_file(conn, content, stem, existing)
            except Exception as exc:
                for table in _existing_tables(conn) - existing:
                    conn.execute(f'DROP TABLE IF EXISTS "{table}"')
                conn.commit()
                results.append(FileResult(filename, ok=False, error=f"Failed to load {filename}: {exc}"))
                continue

            conn.commit()
            for table in new_tables:
                table_sources[table] = filename
            results.append(FileResult(filename, ok=True, tables_added=new_tables))

        conn.commit()
    finally:
        conn.close()

    if not table_sources:
        raise IngestError(
            "None of the uploaded files loaded successfully: "
            + "; ".join(r.error or "" for r in results if not r.ok)
        )

    return db_path, table_sources, results

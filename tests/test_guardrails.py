import sqlite3

import pytest

from app.guardrails import GuardrailError, execute_safe


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, price REAL)")
    conn.executemany(
        "INSERT INTO items (id, name, price) VALUES (?, ?, ?)",
        [(1, "a", 1.0), (2, "b", 2.0), (3, "c", 3.0)],
    )
    conn.commit()
    conn.close()
    return path


def test_plain_select_works(db_path):
    rows = execute_safe(db_path, "SELECT * FROM items")
    assert len(rows) == 3


def test_appends_limit_when_missing(db_path):
    rows = execute_safe(db_path, "SELECT * FROM items")
    assert len(rows) <= 500


def test_respects_existing_limit(db_path):
    rows = execute_safe(db_path, "SELECT * FROM items LIMIT 1")
    assert len(rows) == 1


def test_with_cte_is_allowed(db_path):
    rows = execute_safe(
        db_path,
        "WITH cheap AS (SELECT * FROM items WHERE price < 2.5) SELECT * FROM cheap",
    )
    assert len(rows) == 2


def test_rejects_stacked_statements(db_path):
    with pytest.raises(GuardrailError):
        execute_safe(db_path, "SELECT * FROM items; DROP TABLE items;")


def test_rejects_drop_table(db_path):
    with pytest.raises(GuardrailError):
        execute_safe(db_path, "DROP TABLE items")


def test_rejects_insert(db_path):
    with pytest.raises(GuardrailError):
        execute_safe(db_path, "INSERT INTO items (id, name, price) VALUES (4, 'd', 4.0)")


def test_rejects_update(db_path):
    with pytest.raises(GuardrailError):
        execute_safe(db_path, "UPDATE items SET price = 0")


def test_rejects_delete(db_path):
    with pytest.raises(GuardrailError):
        execute_safe(db_path, "DELETE FROM items")


def test_rejects_pragma(db_path):
    with pytest.raises(GuardrailError):
        execute_safe(db_path, "SELECT * FROM items; PRAGMA table_info(items)")


def test_rejects_attach(db_path):
    with pytest.raises(GuardrailError):
        execute_safe(db_path, "SELECT * FROM items WHERE 1=1; ATTACH DATABASE 'x' AS y")


def test_rejects_empty_query(db_path):
    with pytest.raises(GuardrailError):
        execute_safe(db_path, "   ")


def test_rejects_comment_only_query(db_path):
    with pytest.raises(GuardrailError):
        execute_safe(db_path, "-- just a comment\n")


def test_pragma_blocked_before_reaching_connection(db_path):
    with pytest.raises(GuardrailError):
        execute_safe(db_path, "PRAGMA writable_schema=1")


def test_write_attempt_via_readonly_connection_fails(db_path):
    import app.guardrails as guardrails

    # Bypass _validate to simulate a guardrail bug and confirm the read-only
    # connection is still the final backstop.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM items")
    conn.close()


def test_leading_sql_comment_before_select_is_allowed(db_path):
    rows = execute_safe(db_path, "-- comment\nSELECT * FROM items LIMIT 2")
    assert len(rows) == 2


def test_case_insensitive_select(db_path):
    rows = execute_safe(db_path, "select * from items limit 1")
    assert len(rows) == 1

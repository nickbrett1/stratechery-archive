"""The population query: which messages the archive mirrors, and their metadata.

The predicate is fixed, not a cursor (BRIEF §3.4). There is no `from_domain`
column on `messages`; it is derived exactly as the analytics `v_messages` view
derives it - `message_recipients` where `recipient_type='from'` joined to
`participants.domain`, falling back to `messages.sender_id` when that yields no
row. On this corpus the fallback fires 0 times, but it is not removed: the
query is the view's definition, and a future message without a `from`
recipient should still be found.

`v_messages` itself is NOT queried. It is a DuckDB view over a Parquet
analytics cache, not a table in the archive (the SQLite file has zero views),
and it carries no raw bytes. This module reproduces its sender CTE against the
archive tables directly, then joins `message_raw`.

The SQL is recorded verbatim in every run file, with its sha256, so a run can be
reproduced even if this file changes later.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

REQUIRED_TABLES = (
    "messages",
    "message_raw",
    "message_recipients",
    "participants",
    "message_bodies",
)

BASE_SQL = """\
WITH ms AS (
  SELECT mr.message_id,
         MIN(p.{email}) AS from_email,
         MIN(p.domain)  AS from_domain
  FROM message_recipients mr
  JOIN participants p ON p.id = mr.participant_id
  WHERE mr.recipient_type = 'from'
  GROUP BY mr.message_id
),
ds AS (
  SELECT m.id AS message_id,
         p.{email} AS from_email,
         p.domain  AS from_domain
  FROM messages m
  JOIN participants p ON p.id = m.sender_id
  WHERE m.sender_id IS NOT NULL
)
SELECT m.id                  AS message_id,
       m.rfc822_message_id,
       m.sent_at,
       m.subject,
       m.size_estimate,
       COALESCE(ms.from_email, ds.from_email, '') AS from_email,
       r.compression,
       r.encryption_version,
       b.body_html
FROM messages m
LEFT JOIN message_raw r ON r.message_id = m.id
LEFT JOIN ms ON ms.message_id = m.id
LEFT JOIN ds ON ds.message_id = m.id AND ms.message_id IS NULL
LEFT JOIN message_bodies b ON b.message_id = m.id
WHERE COALESCE(ms.from_domain, ds.from_domain, '') = 'stratechery.com'
ORDER BY m.id
"""


class SchemaError(RuntimeError):
    """The archive schema is not what the query expects.

    Raised with the offending object named, because the alternative - a
    ``no such column`` traceback from deep inside sqlite - reads like a bug in
    this project rather than a msgvault upgrade that needs a query change.
    """


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def assert_schema(conn: sqlite3.Connection) -> None:
    """Fail loudly, naming the object, if the archive is not the expected shape."""
    for table in REQUIRED_TABLES:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            raise SchemaError(f"msgvault.db has no table {table!r}")

    participant_columns = _columns(conn, "participants")
    if "domain" not in participant_columns:
        raise SchemaError(
            "participants has no 'domain' column; the population predicate cannot "
            f"resolve from_domain (columns: {', '.join(participant_columns)})"
        )
    for candidate in ("email", "address"):
        if candidate in participant_columns:
            return
    raise SchemaError(
        "participants has no email-like column ('email' or 'address'); the index "
        f"'from' field cannot be filled (columns: {', '.join(participant_columns)})"
    )


def _email_column(conn: sqlite3.Connection) -> str:
    columns = _columns(conn, "participants")
    return "email" if "email" in columns else "address"


def population_sql(conn: sqlite3.Connection) -> str:
    """The verbatim query for this database, resolved against its schema."""
    assert_schema(conn)
    return BASE_SQL.format(email=_email_column(conn))


def query_sha256(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def iter_population(conn: sqlite3.Connection) -> tuple[str, list[sqlite3.Row]]:
    """Return (verbatim SQL, rows) for the population, ordered by message_id."""
    sql = population_sql(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql).fetchall()
    return sql, rows


def count(conn: sqlite3.Connection) -> int:
    """Population size, for the dry-run preview."""
    sql = population_sql(conn)
    return int(conn.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0])


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}

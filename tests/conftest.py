"""A synthesised msgvault archive. Never a real .eml (BRIEF §5).

Every message carries a per-recipient bearer token in its image URLs. A real
capture would be flagged by gitleaks - correctly - and the build would go red
for a reason that looks like a bug and is the design working. So the fixture is
hand-written: a fake token, one quoted-printable soft break, one base64 part,
one `<img src>` with a dummy query string. That exercises every branch.
"""

from __future__ import annotations

import base64
import sqlite3
import zlib
from pathlib import Path

FAKE_TOKEN = "FAKE-JWT-NOT-A-SECRET"

SCHEMA = """
CREATE TABLE participants (
    id INTEGER PRIMARY KEY,
    email TEXT,
    name TEXT,
    domain TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    rfc822_message_id TEXT,
    sent_at DATETIME,
    subject TEXT,
    sender_id INTEGER REFERENCES participants(id),
    size_estimate INTEGER,
    deleted_at DATETIME,
    deleted_from_source_at DATETIME
);
CREATE TABLE message_raw (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    raw_data BLOB NOT NULL,
    raw_format TEXT NOT NULL,
    compression TEXT DEFAULT 'zlib',
    encryption_version INTEGER DEFAULT 0
);
CREATE TABLE message_recipients (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    recipient_type TEXT NOT NULL
);
CREATE TABLE message_bodies (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    body_text TEXT,
    body_html TEXT
);
"""

HTML = (
    '<html><body><table><tr><td>'
    f'<img src="https://track.stratechery.com/pixel.png?token={FAKE_TOKEN}">'
    '<img src="https://cdn.stratechery.com/logo.png">'
    '<img src="https://cdn.stratechery.com/logo.png">'
    "</td></tr></table></body></html>"
)


def make_eml(message_id: int, subject: str = "Weekly Briefing") -> bytes:
    """A minimal but branch-complete RFC 5322 message, as stored bytes."""
    html_b64 = base64.encodebytes(HTML.encode("utf-8")).decode("ascii")
    head = (
        "Delivered-To: nick@example.com\r\n"
        "From: Stratechery <email@stratechery.com>\r\n"
        "To: nick@example.com\r\n"
        f"Subject: {subject}\r\n"
        "Date: Tue, 03 Nov 2026 11:01:05 +0000\r\n"
        f"Message-ID: <message-{message_id}@mail.stratechery.com>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/alternative; boundary="b1"\r\n'
        "\r\n"
        "--b1\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n"
        "\r\n"
        "Hello, this is a deliberately long line that is soft-wrapped with=\r\n"
        " a quoted-printable break.\r\n"
        "\r\n"
        "--b1\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        f"{html_b64}"
        "\r\n"
        "--b1--\r\n"
    )
    return head.encode("ascii")


def build_vault(
    path: Path,
    message_ids: list[int] | None = None,
    *,
    domain: str = "stratechery.com",
    domain_via_sender_id: bool = False,
) -> sqlite3.Connection:
    """Create a msgvault-shaped SQLite file and return an open connection."""
    message_ids = message_ids if message_ids is not None else [3, 1, 2]
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    conn.execute(
        "INSERT INTO participants (id, email, name, domain) VALUES (1, ?, ?, ?)",
        ("email@stratechery.com", "Stratechery", domain),
    )
    conn.execute(
        "INSERT INTO participants (id, email, name, domain) VALUES (2, ?, ?, ?)",
        ("nick@example.com", "Nick", "example.com"),
    )

    for message_id in message_ids:
        eml = make_eml(message_id)
        sender_id = 1 if domain_via_sender_id else None
        conn.execute(
            "INSERT INTO messages (id, rfc822_message_id, sent_at, subject, sender_id,"
            " size_estimate) VALUES (?, ?, ?, ?, ?, ?)",
            (
                message_id,
                f"<message-{message_id}@mail.stratechery.com>",
                "2026-11-03 11:01:05+00:00",
                f"Weekly Briefing {message_id}",
                sender_id,
                len(eml),
            ),
        )
        conn.execute(
            "INSERT INTO message_raw (message_id, raw_data, raw_format) VALUES (?, ?, ?)",
            (message_id, zlib.compress(eml), "mime"),
        )
        if not domain_via_sender_id:
            conn.execute(
                "INSERT INTO message_recipients (message_id, participant_id, recipient_type)"
                " VALUES (?, ?, 'from')",
                (message_id, 1),
            )
        conn.execute(
            "INSERT INTO message_bodies (message_id, body_text, body_html) VALUES (?, ?, ?)",
            (message_id, "plain text", HTML),
        )

    conn.commit()
    return conn

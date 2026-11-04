"""Image URLs referenced by a message's stored HTML.

Not a parser. This is a substring scan for ``<img ... src="...">`` over the
``message_bodies.body_html`` column that msgvault stores, and it does exactly
one transformation: drop the query string. No entity decoding, no whitespace
collapsing, no lowercasing, no percent-decoding, no trailing-slash trimming.

That restraint is the contract (BRIEF §4 Task 3). ``url_path`` is recorded
exactly as it appears before the ``?`` so a consumer that strips only the
query joins on the same string. Normalising here would create a second
interpretation of the same URL and silently break that join.

Why the DB column rather than the .eml: the archive stores bytes but does not
open them, and finding the ``text/html`` part inside a
``multipart/mixed``(60%)/``multipart/alternative``(40%) document is a MIME
walk - the rejected design. msgvault already flattened the HTML part, for
100% of this corpus, so that is what the index reads.
"""

from __future__ import annotations

import re

# Attribute order varies, so `<img` is non-greedy up to the first `src=`.
# Both quote styles; anything unquoted is deliberately not matched rather than
# guessed at.
_IMG_SRC_RE = re.compile(
    r"""<img\b[^>]*?\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)')""",
    re.IGNORECASE,
)


def image_urls(body_html: str | None) -> list[str]:
    """Distinct ``url_path`` values in document order, query string removed.

    Duplicates within one message collapse: the index records which assets a
    message references, and a logo repeated three times is one reference. Total
    occurrences stay recoverable from the .eml, which is the archive.
    """
    if not body_html:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for match in _IMG_SRC_RE.finditer(body_html):
        src = match.group(1) if match.group(1) is not None else match.group(2)
        url_path = src.split("?", 1)[0]
        if not url_path or url_path in seen:
            continue
        seen.add(url_path)
        out.append(url_path)
    return out

"""Vendored tests for the msgvault REST client (forked from game-catalog).

These are the upstream tests minus the game-catalog-specific ones (Best Buy
receipt parsing, item classification), which do not exist here. Kept with the
code on purpose: a fork without its tests is a copy nobody can maintain.

Upstream: nickbrett1/game-catalog tests/test_msgvault_client.py
Blob:     54973efc6042d638ddc6cfdd451eab1aa7877a92
"""

from __future__ import annotations

import httpx
import pytest

from stratechery_archive.msgvault import MsgvaultClient, recover_webview_html

MSGS = [
    {
        "id": 30,
        "subject": "Thank You For Your Purchase",
        "from_email": "sony@email.sonyentertainmentnetwork.com",
        "sent_at": "2022-12-14T13:33:42Z",
    },
    {
        "id": 20,
        "subject": "Thank You For Your Purchase",
        "from_email": "sony@email.sonyentertainmentnetwork.com",
        "sent_at": "2022-12-01T10:00:00Z",
    },
    {
        "id": 10,
        "subject": "Some Other Mail",
        "from_email": "sony@email.sonyentertainmentnetwork.com",
        "sent_at": "2022-11-01T10:00:00Z",
    },
]


def _client(handler) -> MsgvaultClient:
    transport = httpx.MockTransport(handler)
    return MsgvaultClient(
        "http://msgvault:8080",
        retries=1,
        client=httpx.Client(base_url="http://msgvault:8080", transport=transport),
    )


def _filter_handler(messages: list[dict], page_size: int = 2):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/messages/filter"
        offset = int(request.url.params.get("offset", "0"))
        chunk = messages[offset : offset + page_size]
        return httpx.Response(
            200,
            json={
                "count": len(messages),
                "has_more": offset + len(chunk) < len(messages),
                "offset": offset,
                "limit": page_size,
                "messages": chunk,
            },
        )

    return handler


def _capture_filter_handler(messages: list[dict], seen: list):
    """Record the query params on each request (asserting `after` is forwarded)."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "count": len(messages),
                "has_more": False,
                "offset": 0,
                "limit": len(messages),
                "messages": messages,
            },
        )

    return handler


def test_search_subject_filter_client_side():
    client = _client(_filter_handler(MSGS))
    out = client.search_messages(
        sender="sony@email.sonyentertainmentnetwork.com",
        subject="Thank You For Your Purchase",
        limit=10,
    )
    assert [m["id"] for m in out] == [30, 20]


def test_after_forwarded_as_date_bound_to_msgvault():
    # `after` is a DATE watermark forwarded to msgvault's native filter, not a
    # message-id stop: the client must not drop low-id-but-recent messages.
    seen: list = []
    client = _client(_capture_filter_handler(MSGS, seen))
    out = client.search_messages(
        sender="sony@email.sonyentertainmentnetwork.com",
        after="2025-12-19T00:00:00Z",
        limit=10,
    )
    assert seen and seen[0].get("after") == "2025-12-19T00:00:00Z"
    assert [m["id"] for m in out] == [30, 20, 10]


def test_after_coerced_to_str_for_msgvault():
    seen: list = []
    client = _client(_capture_filter_handler(MSGS, seen))
    client.search_messages(sender="x@y.z", after=20251219, limit=10)
    assert seen and seen[0].get("after") == "20251219"


def test_low_id_recent_date_not_dropped_by_high_cursor():
    low_id_recent = [
        {
            "id": 10172,
            "subject": "Thank you for your order!",
            "from_email": "notifications@info.gamestop.com",
            "sent_at": "2025-12-19T01:24:34Z",
        },
        {
            "id": 42957,
            "subject": "Thank you for your order!",
            "from_email": "notifications@info.gamestop.com",
            "sent_at": "2023-06-23T01:51:22Z",
        },
    ]
    client = _client(_filter_handler(low_id_recent))
    out = client.search_messages(
        sender="notifications@info.gamestop.com",
        after="2023-06-23T00:00:00Z",
        limit=10,
    )
    assert [m["id"] for m in out] == [10172, 42957]


def test_paging_across_pages():
    client = _client(_filter_handler(MSGS, page_size=2))
    out = client.search_messages(
        sender="sony@email.sonyentertainmentnetwork.com", limit=10
    )
    assert [m["id"] for m in out] == [30, 20, 10]


def test_get_message_returns_body_and_body_html():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/messages/53008"
        return httpx.Response(
            200,
            json={
                "id": 53008,
                "subject": "Thank You For Your Purchase",
                "from_email": "sony@email.sonyentertainmentnetwork.com",
                "sent_at": "2022-12-14T13:33:42Z",
                "has_attachments": False,
                "body": "plain text receipt",
                "body_html": "<html>receipt</html>",
            },
        )

    msg = _client(handler).get_message(53008)
    assert msg["id"] == 53008
    assert msg["body_text"] == "plain text receipt"
    assert msg["body_html"] == "<html>receipt</html>"
    assert msg["from_email"].startswith("sony@")


def test_get_stats():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"total_messages": 121875, "active_messages": 121875}
        )

    stats = _client(handler).get_stats()
    assert stats["total_messages"] == 121875


def test_5xx_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"total_messages": 1})

    # retries=1 -> no retry: the 500 surfaces immediately.
    client = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_stats()

    # retries=3 -> the transient 500 is retried and the call succeeds.
    calls["n"] = 0
    client2 = MsgvaultClient(
        "http://msgvault:8080",
        retries=3,
        client=httpx.Client(
            base_url="http://msgvault:8080", transport=httpx.MockTransport(handler)
        ),
    )
    assert client2.get_stats()["total_messages"] == 1
    assert calls["n"] == 2


def test_429_rate_limit_retries():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"total_messages": 1})

    client = MsgvaultClient(
        "http://msgvault:8080",
        retries=3,
        client=httpx.Client(
            base_url="http://msgvault:8080", transport=httpx.MockTransport(handler)
        ),
    )
    assert client.get_stats()["total_messages"] == 1
    assert calls["n"] == 2


def test_recover_webview_html_fetches_webview_link():
    html = "<html>DRAGON QUEST III HD-2D Remake - PlayStation 5</html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    client = _client(handler)
    stub = "View as a Web page:\r\nhttps://click.emailinfo2.bestbuy.com/?qs=ABC123\r\n"
    assert recover_webview_html(stub, client=client) == html


def test_recover_webview_html_none_when_no_link():
    assert recover_webview_html("no link here") is None

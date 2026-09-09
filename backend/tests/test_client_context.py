from __future__ import annotations

from starlette.requests import Request

from app.services.client_context import client_context_from_request, normalize_client_context


def _request(*, client: str, user_agent: str, forwarded_for: str = "") -> Request:
    headers = [(b"user-agent", user_agent.encode("latin-1"))]
    if forwarded_for:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (client, 43210),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


def test_client_context_uses_proxy_normalized_peer_and_parses_desktop_browser() -> None:
    request = _request(
        client="203.0.113.42",
        forwarded_for="198.51.100.99",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36 Edg/140.0.3485.54"
        ),
    )

    assert client_context_from_request(request) == {
        "ip_address": "203.0.113.42",
        "browser": "Microsoft Edge",
        "browser_version": "140.0.3485.54",
        "operating_system": "Windows 10/11",
        "device_type": "DESKTOP",
    }


def test_client_context_parses_mobile_safari_and_ipv6() -> None:
    request = _request(
        client="2001:db8::7",
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 "
            "Mobile/15E148 Safari/604.1"
        ),
    )

    assert client_context_from_request(request) == {
        "ip_address": "2001:db8::7",
        "browser": "Safari",
        "browser_version": "18.6",
        "operating_system": "iOS 18.6",
        "device_type": "MOBILE",
    }


def test_client_context_discards_unknown_fields_and_bounds_persisted_values() -> None:
    context = normalize_client_context(
        {
            "ip_address": " 192.0.2.10 ",
            "browser": " Browser   Name ",
            "browser_version": "1" * 100,
            "unexpected_fingerprint": "must not be exposed",
        }
    )

    assert context == {
        "ip_address": "192.0.2.10",
        "browser": "Browser Name",
        "browser_version": "1" * 32,
    }

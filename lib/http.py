"""Tiny zero-dependency HTTP helper (stdlib urllib).

Compile-mode Ignite functions deploy cleanest with no pip requirements, so we use
urllib instead of httpx/requests. Returns (status_code, raw_bytes); callers decode.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


def request(
    method: str,
    url: str,
    headers: dict | None = None,
    json_body: object = None,
    form_body: dict | None = None,
    data: bytes | None = None,
    timeout: float = 60.0,
) -> tuple[int, bytes]:
    """One HTTP call. Body precedence: json_body > form_body > data."""
    headers = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers.setdefault("Content-Type", "application/json")
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def request_json(method: str, url: str, **kw) -> tuple[int, object]:
    """Like request() but parse the response body as JSON (or {} on non-JSON)."""
    status, body = request(method, url, **kw)
    try:
        return status, json.loads(body) if body else {}
    except (ValueError, TypeError):
        return status, {"_raw": body.decode("utf-8", "replace")}

from cos2pag.http_client import ApiError, request_json


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self.content = text.encode() if json_body is None and text else b"{}" if json_body is not None else b""

    def json(self):
        return self._json_body


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def test_request_json_returns_parsed_body():
    session = FakeSession(FakeResponse(200, json_body={"ok": True}))
    result = request_json(session, "GET", "https://example.com/x")
    assert result == {"ok": True}
    assert len(session.calls) == 1


def test_request_json_raises_api_error_on_4xx():
    session = FakeSession(FakeResponse(404, text="not found"))
    try:
        request_json(session, "GET", "https://example.com/x")
        assert False, "expected ApiError"
    except ApiError as exc:
        assert exc.status_code == 404


def test_request_json_dry_run_skips_mutating_request():
    session = FakeSession(FakeResponse(200, json_body={"ok": True}))
    result = request_json(session, "PATCH", "https://example.com/x", json={"a": 1}, dry_run=True)
    assert result is None
    assert session.calls == []


def test_request_json_dry_run_still_performs_get():
    session = FakeSession(FakeResponse(200, json_body={"ok": True}))
    result = request_json(session, "GET", "https://example.com/x", dry_run=True)
    assert result == {"ok": True}
    assert len(session.calls) == 1

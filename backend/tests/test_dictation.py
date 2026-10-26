import io
import json
from urllib.error import HTTPError, URLError

import pytest

import app as application
import dictation


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SONIOX_API_KEY", "permanent-test-secret")
    with application.app.test_client() as client:
        with client.session_transaction() as session:
            session["auth"] = True
        yield client


def test_requires_authentication():
    with application.app.test_client() as client:
        assert client.post("/api/dictation/session").status_code == 401


def test_rejects_cross_origin_form(client):
    assert client.post("/api/dictation/session").status_code == 400


def test_missing_configuration(client, monkeypatch):
    monkeypatch.delenv("SONIOX_API_KEY")
    response = client.post("/api/dictation/session", headers={"X-Brief-Dictation": "1"})
    assert response.status_code == 503
    assert response.json["error"] == "dictation_unavailable"
    assert application.app.test_client().get("/api/config").status_code == 401
    assert client.get("/api/config").json["dictation"]["enabled"] is False


def test_mints_single_use_credentials_without_exposing_permanent_key(client, monkeypatch):
    def mint(request, timeout):
        assert request.full_url == "https://api.soniox.com/v1/auth/temporary-api-key"
        assert timeout == 10
        assert request.get_header("Authorization") == "Bearer permanent-test-secret"
        assert json.loads(request.data) == {
            "usage_type": "transcribe_websocket", "expires_in_seconds": 60, "single_use": True,
        }
        return io.BytesIO(b'{"api_key":"temporary-test-key","expires_at":"soon"}')

    monkeypatch.setattr(dictation, "urlopen", mint)
    response = client.post("/api/dictation/session", headers={"X-Brief-Dictation": "1"})
    assert response.status_code == 200
    assert response.json == {"api_key": "temporary-test-key", "expires_at": "soon"}
    assert response.headers["Cache-Control"] == "no-store"
    assert b"permanent-test-secret" not in response.data
    assert b"permanent-test-secret" not in client.get("/api/config").data


@pytest.mark.parametrize("failure", [
    HTTPError("url", 402, "secret provider response", {}, None),
    HTTPError("url", 403, "secret provider response", {}, None),
    URLError("secret provider response"), TimeoutError("secret provider response"),
    ValueError("secret provider response"),
])
def test_provider_failures_are_safe(client, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(dictation, "urlopen", fail)
    response = client.post("/api/dictation/session", headers={"X-Brief-Dictation": "1"})
    assert response.status_code == 503
    assert b"secret provider response" not in response.data


@pytest.mark.parametrize("body", [b'{}', b'null', b'{"api_key":"permanent-test-secret"}', b'bad json'])
def test_invalid_provider_credentials_are_not_returned(client, monkeypatch, body):
    monkeypatch.setattr(dictation, "urlopen", lambda *args, **kwargs: io.BytesIO(body))
    assert client.post("/api/dictation/session", headers={"X-Brief-Dictation": "1"}).status_code == 503

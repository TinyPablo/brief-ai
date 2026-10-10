"""Share-link routes: token issuing/revoking, the public read route, and the
cross-prompt guard on shared images."""

import app as appmod
import pytest


class _ScriptedConn:
    """Stands in for a DB connection, replaying canned fetchone/fetchall
    results in call order - the routes under test always query in a fixed,
    known sequence, so this avoids standing up a real Postgres."""

    def __init__(self, *, ones=(), alls=()):
        self.calls = []
        self._ones = list(ones)
        self._alls = list(alls)

    def cursor(self, *a, **k):
        return self

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._ones.pop(0) if self._ones else None

    def fetchall(self):
        return self._alls.pop(0) if self._alls else []

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def _client(conn, monkeypatch, authed=True):
    monkeypatch.setattr(appmod, "get_db", lambda: conn)
    client = appmod.app.test_client()
    if authed:
        with client.session_transaction() as sess:
            sess["auth"] = True
    return client


# --- sharing / unsharing ----------------------------------------------------

def test_sharing_an_unshared_prompt_generates_and_stores_a_token(monkeypatch):
    conn = _ScriptedConn(ones=[(None, True)])
    res = _client(conn, monkeypatch).post("/api/history/1/share")
    assert res.status_code == 200
    token = res.get_json()["token"]
    assert token
    update = [c for c in conn.calls if c[0].startswith("UPDATE prompts SET share_token")]
    assert update and update[0][1] == (token, 1)


def test_sharing_an_already_shared_prompt_returns_the_same_token(monkeypatch):
    conn = _ScriptedConn(ones=[("existing-token", True)])
    res = _client(conn, monkeypatch).post("/api/history/1/share")
    assert res.get_json()["token"] == "existing-token"
    assert not [c for c in conn.calls if c[0].startswith("UPDATE")]


def test_sharing_a_missing_prompt_is_a_404(monkeypatch):
    conn = _ScriptedConn(ones=[None])
    res = _client(conn, monkeypatch).post("/api/history/999/share")
    assert res.status_code == 404


@pytest.mark.parametrize("token", [None, "a" * 22])
def test_legacy_prompt_cannot_be_shared_even_with_an_existing_token(monkeypatch, token):
    conn = _ScriptedConn(ones=[(token, False)])
    res = _client(conn, monkeypatch).post("/api/history/1/share")
    assert res.status_code == 409
    assert "personal context" in res.get_json()["error"]
    assert not any(sql.startswith("UPDATE") for sql, _ in conn.calls)


def test_unsharing_clears_the_token(monkeypatch):
    conn = _ScriptedConn()
    res = _client(conn, monkeypatch).delete("/api/history/1/share")
    assert res.status_code == 200
    clear = [c for c in conn.calls if c[0].startswith("UPDATE prompts SET share_token")]
    assert clear and "share_token = NULL" in clear[0][0] and clear[0][1] == (1,)


def test_share_routes_require_auth(monkeypatch):
    conn = _ScriptedConn()
    client = _client(conn, monkeypatch, authed=False)
    assert client.post("/api/history/1/share").status_code == 401
    assert client.delete("/api/history/1/share").status_code == 401


# --- public read route -------------------------------------------------------

_ROW = {
    "id": 1,
    "created_at": __import__("datetime").datetime(2026, 1, 1),
    "model": "gemini-3.1-flash-lite",
    "prompt": "what is the capital of poland",
    "answer": "Warsaw.",
    "prompt_is_raw": True,
}


def test_unknown_token_is_a_404(monkeypatch):
    conn = _ScriptedConn(ones=[None])
    monkeypatch.setattr(appmod, "get_db", lambda: conn)
    res = appmod.app.test_client().get("/api/share/doesnotexist12345")
    assert res.status_code == 404


def test_existing_legacy_share_link_does_not_expose_personal_context(monkeypatch):
    row = {**_ROW, "prompt_is_raw": False, "prompt": "PRIVATE CONTEXT\n\nQuestion"}
    conn = _ScriptedConn(ones=[row])
    monkeypatch.setattr(appmod, "get_db", lambda: conn)
    res = appmod.app.test_client().get("/api/share/" + "a" * 22)
    assert res.status_code == 404
    assert res.get_json() == {"error": "not_found"}
    assert len(conn.calls) == 1


def test_malformed_token_never_touches_the_database(monkeypatch):
    def explode():
        raise AssertionError("the database must not be reached for a malformed token")
    monkeypatch.setattr(appmod, "get_db", explode)
    res = appmod.app.test_client().get("/api/share/../../etc/passwd")
    assert res.status_code == 404


def test_shared_payload_has_no_auth_required(monkeypatch):
    conn = _ScriptedConn(ones=[_ROW], alls=[[]])
    monkeypatch.setattr(appmod, "get_db", lambda: conn)
    res = appmod.app.test_client().get("/api/share/" + "a" * 22)
    assert res.status_code == 200


def test_shared_payload_excludes_internal_fields(monkeypatch):
    conn = _ScriptedConn(ones=[_ROW], alls=[[]])
    monkeypatch.setattr(appmod, "get_db", lambda: conn)
    body = appmod.app.test_client().get("/api/share/" + "a" * 22).get_json()
    assert set(body.keys()) == {"created_at", "model_label", "prompt", "answer", "images"}
    for leaked in ("context", "cost_usd", "input_tokens", "output_tokens",
                   "duration_ms", "reasoning", "id", "share_token", "brief"):
        assert leaked not in body


# --- image cross-prompt guard -------------------------------------------------

def test_shared_image_for_a_different_prompt_is_a_404(monkeypatch):
    # _shared_prompt_id resolves the token, then the ownership check finds no row.
    conn = _ScriptedConn(ones=[(1, True), None])
    monkeypatch.setattr(appmod, "get_db", lambda: conn)
    res = appmod.app.test_client().get("/api/share/" + "a" * 22 + "/images/" + "b" * 64)
    assert res.status_code == 404


@pytest.mark.parametrize("suffix", ["", "/thumb"])
def test_existing_legacy_share_cannot_serve_images(monkeypatch, suffix):
    conn = _ScriptedConn(ones=[(1, False)])
    monkeypatch.setattr(appmod, "get_db", lambda: conn)
    res = appmod.app.test_client().get(
        "/api/share/" + "a" * 22 + "/images/" + "b" * 64 + suffix
    )
    assert res.status_code == 404
    assert len(conn.calls) == 1


def test_unknown_share_token_on_image_route_is_a_404(monkeypatch):
    conn = _ScriptedConn(ones=[None])
    monkeypatch.setattr(appmod, "get_db", lambda: conn)
    res = appmod.app.test_client().get("/api/share/" + "a" * 22 + "/images/" + "b" * 64)
    assert res.status_code == 404

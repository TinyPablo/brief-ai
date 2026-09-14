"""What /api/generate sends to the provider and what it writes to the row are
deliberately different things. These tests hold that line."""

import pytest

import app as appmod

CONTEXT = "user is currently in Bielsko-Biała\nuser data: 20yo, 181cm"


class _RecordingCursor:
    """Captures the INSERT so a test can assert on the stored columns."""

    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        if params and "INSERT INTO prompts" in sql:
            self.sink["sql"] = sql
            self.sink["params"] = params

    def fetchone(self):
        import datetime
        return (1, datetime.datetime(2026, 9, 14, 11, 20))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def sent_and_stored(monkeypatch):
    """Stubs the provider call and the DB, returning both sides of the request."""
    captured = {}

    def fake_call(model, prompt, depth, images=None):
        captured["prompt_to_model"] = prompt
        return {
            "answer": "ok", "input_tokens": 10, "output_tokens": 20,
            "stop_reason": "end_turn", "served": model,
        }

    monkeypatch.setattr(appmod, "call_gemini", fake_call)
    monkeypatch.setattr(appmod, "call_openai", fake_call)
    monkeypatch.setattr(appmod, "call_anthropic", fake_call)

    class _Conn:
        def cursor(self, *a, **k):
            return _RecordingCursor(captured)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close(self):
            pass

    monkeypatch.setattr(appmod, "get_db", lambda: _Conn())
    return captured


def _post(body):
    client = appmod.app.test_client()
    with client.session_transaction() as sess:
        sess["auth"] = True
    return client.post("/api/generate", json=body)


def _stored(captured):
    """Maps the INSERT params onto the column names in the statement."""
    params = captured["params"]
    return {
        "model": params[0], "prompt": params[1], "context": params[2],
        "brief": params[3], "answer": params[4],
    }


def test_model_sees_the_composed_text(sent_and_stored):
    res = _post({"model": "claude-haiku-4-5", "prompt": "jak to działa", "context": CONTEXT})
    assert res.status_code == 200
    assert sent_and_stored["prompt_to_model"] == CONTEXT + "\n\njak to działa"


def test_row_stores_the_parts_separately(sent_and_stored):
    _post({"model": "claude-haiku-4-5", "prompt": "jak to działa", "context": CONTEXT, "brief": True})
    stored = _stored(sent_and_stored)
    assert stored["prompt"] == "jak to działa"
    assert stored["context"] == CONTEXT
    assert stored["brief"] is True
    assert "very brief" not in stored["prompt"]
    assert "Bielsko" not in stored["prompt"]


def test_absent_context_is_stored_as_null_not_empty_string(sent_and_stored):
    """NULL and '' must not both mean "no context"; the endpoint normalises to
    NULL so the column has one spelling for the empty case."""
    _post({"model": "claude-haiku-4-5", "prompt": "jak to działa"})
    assert _stored(sent_and_stored)["context"] is None


def test_new_rows_are_marked_as_holding_a_raw_prompt(sent_and_stored):
    _post({"model": "claude-haiku-4-5", "prompt": "x"})
    assert "prompt_is_raw" in sent_and_stored["sql"]
    assert "TRUE" in sent_and_stored["sql"]


def test_oversized_context_is_refused(sent_and_stored):
    res = _post({
        "model": "claude-haiku-4-5", "prompt": "x",
        "context": "a" * (appmod.MAX_CONTEXT_CHARS + 1),
    })
    assert res.status_code == 400
    assert res.get_json()["error"] == "context_too_long"
    assert "prompt_to_model" not in sent_and_stored  # refused before the provider call

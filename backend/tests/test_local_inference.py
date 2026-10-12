import fcntl
import json
import subprocess

import httpx
import pytest

import app as appmod
import local_inference as local


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_AI_URL", "http://local-ai:8080")
    monkeypatch.setenv("LOCAL_AI_LOCK", str(tmp_path / "inference.lock"))
    monkeypatch.setenv("LOCAL_AI_API_KEY", "test-local-key")
    return tmp_path / "inference.lock"


def test_context_budget_includes_template_and_output():
    calls = []
    def handler(request):
        if request.url.path == "/slots":
            return httpx.Response(200, json=[])
        calls.append(request.url.path)
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "rendered template"})
        assert json.loads(request.content)["content"] == "rendered template"
        return httpx.Response(200, json={"tokens": [1] * 8000})
    with httpx.Client(base_url="http://local", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(local.LocalError, match="context") as error:
            local.infer(client, {"messages": [{"role": "user", "content": "code"}], "max_tokens": 1024})
    assert error.value.status == 400
    assert calls == ["/apply-template", "/tokenize"]


def test_inference_uses_fixed_model_and_preserves_usage():
    def handler(request):
        if request.url.path == "/slots":
            return httpx.Response(200, json=[])
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "template"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1, 2]})
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 20
        assert payload["stream"] is True
        assert payload["sse_ping_interval"] == 1
        assert payload["model"] == local.MODEL_ID
        chunks = [
            {"id": "test", "choices": [{"delta": {"content": "hello"}, "finish_reason": None}]},
            {"id": "test", "choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2, "completion_tokens": 10}},
        ]
        stream = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=stream)
    with httpx.Client(base_url="http://local", transport=httpx.MockTransport(handler)) as client:
        result = local.infer(client, {"messages": [{"role": "user", "content": "code"}], "max_tokens": 20})
    assert result["model"] == local.MODEL_ID
    assert result["choices"][0]["message"]["content"] == "hello"
    assert result["usage"]["completion_tokens"] == 10


def test_busy_lock_rejects_before_starting_worker(enabled, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    with enabled.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(local.LocalError) as error:
            local.generate([{"role": "user", "content": "hello"}])
    assert error.value.status == 429


def test_timeout_releases_lock_and_does_not_retry(enabled, monkeypatch):
    calls = []
    def timed_out(*a, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired("worker", local.DEADLINE)
    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(local.LocalError) as error:
        local.generate([{"role": "user", "content": "hello"}])
    assert error.value.status == 504
    assert len(calls) == 1 and calls[0]["timeout"] == 240
    with enabled.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("messages", [None, [], ["text"], [{"role": "user", "content": []}], [{"role": "tool", "content": "x"}]])
def test_invalid_messages(enabled, messages):
    with pytest.raises(local.LocalError) as error:
        local.generate(messages)
    assert error.value.status == 400


def test_external_api_requires_own_key_even_with_browser_session(enabled):
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session["auth"] = True
    assert client.get("/api/local/v1/models").status_code == 401
    assert client.get("/api/local/v1/models", headers={"Authorization": "Bearer test-local-key"}).status_code == 200


def test_external_api_uses_bounded_gateway(enabled, monkeypatch):
    def busy(*args):
        raise local.LocalError("local_busy", "Busy", 429)
    monkeypatch.setattr(local, "generate", busy)
    response = appmod.app.test_client().post("/api/local/v1/chat/completions",
        headers={"Authorization": "Bearer test-local-key"},
        json={"model": local.MODEL_ID, "messages": [{"role": "user", "content": "code"}]})
    assert response.status_code == 429
    assert response.json["error"]["type"] == "local_busy"


def test_external_api_rejects_streaming(enabled):
    response = appmod.app.test_client().post("/api/local/v1/chat/completions",
        headers={"Authorization": "Bearer test-local-key"},
        json={"model": local.MODEL_ID, "messages": [], "stream": True})
    assert response.status_code == 400


def test_local_ui_rejects_images_before_inference(enabled, monkeypatch):
    monkeypatch.setattr(appmod, "SELECTABLE_MODELS", appmod.SELECTABLE_MODELS | {local.MODEL_ID})
    monkeypatch.setattr(appmod, "validate_images", lambda data: (None, [{"data": "image"}]))
    monkeypatch.setattr(local, "generate", lambda *a, **k: pytest.fail("must not run"))
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session["auth"] = True
    response = client.post("/api/generate", json={"model": local.MODEL_ID, "prompt": "explain"})
    assert response.status_code == 400
    assert response.json["error"] == "images_not_supported"

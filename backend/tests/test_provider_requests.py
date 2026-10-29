from types import SimpleNamespace as NS

import httpx
import pytest

import provider_requests as requests


class Rejected(Exception):
    def __init__(self, status, body=None):
        super().__init__('private upstream message')
        self.status_code, self.body = status, body


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(requests.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(requests.time, 'sleep', lambda delay: now.__setitem__(0, now[0] + delay))
    return now


def test_shared_budget_includes_all_tool_rounds(clock):
    timeouts = []
    def send(seconds):
        timeouts.append(seconds)
        clock[0] += 70
        return 'reply'
    @requests.bounded
    def generate():
        requests.request(send)
        requests.request(send)
    with pytest.raises(requests.ProviderError) as error:
        generate()
    assert error.value.code == 'provider_timeout'
    assert timeouts == [120, 50]


def test_retry_budget_is_shared_across_rounds(clock):
    attempts = []
    def send(seconds):
        attempts.append(seconds)
        if len(attempts) != 2:
            raise Rejected(503)
        return 'first reply'
    @requests.bounded
    def generate():
        requests.request(send)
        requests.request(send)
    with pytest.raises(requests.ProviderError) as error:
        generate()
    assert error.value.code == 'provider_busy'
    assert len(attempts) == 3 and attempts == [120, 119, 119]


@pytest.mark.parametrize('failure', [httpx.ReadTimeout('private message'), Rejected(400), Rejected(401), Rejected(403), Rejected(404), Rejected(429, {'code': 'insufficient_quota'})])
def test_ambiguous_timeouts_and_permanent_failures_are_not_retried(clock, failure):
    attempts = []
    def send(seconds):
        attempts.append(seconds)
        raise failure
    with pytest.raises(requests.ProviderError) as error:
        requests.request(send)
    assert len(attempts) == 1
    assert 'private' not in error.value.detail


def test_a_new_generation_gets_a_new_budget(clock):
    @requests.bounded
    def generate():
        return requests.request(lambda seconds: seconds)
    assert generate() == 120
    clock[0] = 200
    assert generate() == 120


def test_google_and_openai_chart_calls_receive_timeouts(clock):
    from unittest.mock import Mock
    from google.genai import types
    from chart_generation import google_generate, openai_generate
    google_call = Mock(return_value=types.GenerateContentResponse())
    google_generate(NS(models=NS(generate_content=google_call)), types, 'model', [],
                    {'system_instruction': 'System', 'max_output_tokens': 100}, {})
    config = google_call.call_args.kwargs['config']
    assert config.http_options.timeout == 120000
    assert config.http_options.retry_options.attempts == 1
    openai_call = Mock(return_value=NS(model='model', output_text='Reply', output=[], status='completed', usage=NS(input_tokens=1, output_tokens=1)))
    openai_generate(NS(responses=NS(create=openai_call)), 'model', [], 'System', 100, None, {})
    assert openai_call.call_args.kwargs['timeout'] == 120


def test_parallel_generations_have_independent_retry_budgets(clock):
    from concurrent.futures import ThreadPoolExecutor
    def generate():
        calls = []
        @requests.bounded
        def run():
            def send(seconds):
                calls.append(seconds)
                if len(calls) == 1:
                    raise Rejected(503)
                return 'ok'
            return requests.request(send)
        return run(), len(calls)
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(lambda _: generate(), range(2))) == [('ok', 2), ('ok', 2)]


@pytest.mark.parametrize('status,code', [(401, 'provider_access'), (403, 'provider_access'),
    (400, 'provider_request'), (404, 'provider_request'), (429, 'provider_rate_limit'),
    (503, 'provider_busy'), (529, 'provider_busy'), (504, 'provider_timeout')])
def test_legacy_endpoint_returns_safe_error_categories(monkeypatch, status, code):
    import app
    def fail(*args, **kwargs):
        raise Rejected(status)
    monkeypatch.setattr(app, 'call_gemini', fail)
    with app.app.test_client() as client:
        with client.session_transaction() as session:
            session['auth'] = True
        response = client.post('/api/generate', json={'prompt': 'Question', 'model': 'gemini-3.1-flash-lite'})
    assert response.json['error'] == code
    assert response.status_code >= 500
    assert 'private' not in response.json['detail']


def test_timeout_does_not_reset_when_chart_generation_is_nested(clock):
    from unittest.mock import Mock
    from chart_generation import openai_generate
    create = Mock(return_value=NS(model='model', output_text='Reply', output=[], status='completed', usage=NS(input_tokens=1, output_tokens=1)))
    @requests.bounded
    def generate():
        clock[0] += 90
        return openai_generate(NS(responses=NS(create=create)), 'model', [], 'System', 100, None, {})
    assert generate()['answer'] == 'Reply'
    assert create.call_args.kwargs['timeout'] == 30

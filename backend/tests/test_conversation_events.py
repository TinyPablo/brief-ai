from uuid import uuid4

import app as api
import conversation_events as events


def test_event_stream_requires_authentication(monkeypatch):
    def unexpected(*args):
        raise AssertionError('Unauthenticated request opened a database listener')
    monkeypatch.setattr(events, 'Listener', unexpected)
    with api.app.test_client() as client:
        assert client.get('/api/conversation-events').status_code == 401


def test_authenticated_stream_sends_ready_then_ids_and_closes_listener(monkeypatch):
    cid = str(uuid4())
    closed = []
    class FakeListener:
        def __init__(self, app, channel):
            assert channel == events.CHANGE_CHANNEL
        def wait(self, timeout):
            return ['invalid', cid]
        def close(self):
            closed.append(True)
    monkeypatch.setattr(events, 'Listener', FakeListener)
    with api.app.test_client() as client:
        with client.session_transaction() as session:
            session['auth'] = True
        response = client.get('/api/conversation-events', buffered=False)
        assert response.mimetype == 'text/event-stream'
        assert response.headers['X-Accel-Buffering'] == 'no'
        assert 'no-cache' in response.headers['Cache-Control']
        iterator = iter(response.response)
        assert b'event: ready' in next(iterator)
        changed = next(iterator)
        assert cid.encode() in changed and b'event: changed' in changed
        assert b'invalid' not in changed
        response.close()
    assert closed == [True]


def test_disconnected_database_signals_fallback_and_releases_connection(monkeypatch):
    closed = []
    class FakeListener:
        def __init__(self, *args):
            pass
        def wait(self, timeout):
            raise OSError('disconnected')
        def close(self):
            closed.append(True)
    monkeypatch.setattr(events, 'Listener', FakeListener)
    with api.app.test_client() as client:
        with client.session_transaction() as session:
            session['auth'] = True
        response = client.get('/api/conversation-events', buffered=False)
        iterator = iter(response.response)
        next(iterator)
        assert b'event: unavailable' in next(iterator)
        response.close()
    assert closed == [True]

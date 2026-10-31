"""Verify transactional wakeups, queue pickup and durable results in isolated Postgres."""

from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time
from uuid import uuid4

import psycopg2


def check():
    schema = 'conversation_events_' + uuid4().hex
    admin = psycopg2.connect(os.environ['DATABASE_URL'])
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    previous = os.environ.get('PGOPTIONS')
    os.environ['PGOPTIONS'] = '-c search_path=' + schema
    try:
        import app as api
        from conversations import database
        from conversation_events import Listener, WORK_CHANNEL, CHANGE_CHANNEL
        from conversation_worker import work
        worker_ready, stop, called = threading.Event(), threading.Event(), threading.Event()
        class ObservedListener(Listener):
            def wait(self, timeout=5):
                worker_ready.set()
                return super().wait(timeout)
        import conversation_worker
        conversation_worker.Listener = ObservedListener
        def fake(model, prompt, depth, **kwargs):
            if not prompt:
                called.set()
            return dict(answer='Immediate answer', charts=[], input_tokens=1, output_tokens=1,
                        stop_reason='end_turn', served=model)
        api.call_gemini = fake
        client = api.app.test_client()
        with client.session_transaction() as session:
            session['auth'] = True
        changes = Listener(api, CHANGE_CHANNEL)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(work, api, stop)
            try:
                assert worker_ready.wait(5), 'Worker never entered notification wait'
                started = time.perf_counter()
                response = client.post('/api/conversations', json={
                    'model': 'gemini-3.1-flash-lite', 'prompt': 'Question', 'request_id': str(uuid4()),
                })
                assert response.status_code == 202, response.json
                assert called.wait(.75), 'Queue pickup still waits for the one-second polling interval'
                cid = response.json['id']
                notified = set()
                end = time.monotonic() + 5
                while time.monotonic() < end:
                    notified.update(changes.wait(.2))
                    result = client.get('/api/conversations/' + cid).json
                    if result['job']['status'] == 'completed':
                        break
                else:
                    raise AssertionError('No completed answer')
                assert cid in notified
                assert result['timing']['queue_ms'] < 750, result['timing']
                assert result['timing']['worker_ms'] is not None
                assert len(result['messages']) == 2
                print(f'Notification pickup + fake answer: {(time.perf_counter()-started)*1000:.1f} ms')
            finally:
                stop.set()
                with admin.cursor() as cur:
                    cur.execute('SELECT pg_notify(%s, %s)', (WORK_CHANNEL, ''))
                future.result(timeout=7)
        # Notifications become visible only when their transaction commits.
        changes.close()
        changes = Listener(api, CHANGE_CHANNEL)
        with database(api) as cur:
            cur.execute('UPDATE conversations SET title=%s WHERE id=%s', ('Committed', cid))
            assert cid not in changes.wait(.05)
        assert cid in changes.wait(1)
        changes.close()
        # A rolled-back mutation must not cause an update event.
        listener = Listener(api, CHANGE_CHANNEL)
        conn = api.get_db()
        with conn.cursor() as cur:
            cur.execute('UPDATE conversations SET title=%s WHERE id=%s', ('Rolled back', cid))
        conn.rollback(); conn.close()
        assert listener.wait(.1) == []
        listener.close()
        print('PASS: immediate queue wakeup, completion events, timing, commit ordering and rollback isolation')
    finally:
        if previous is None:
            os.environ.pop('PGOPTIONS', None)
        else:
            os.environ['PGOPTIONS'] = previous
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


if __name__ == '__main__':
    check()

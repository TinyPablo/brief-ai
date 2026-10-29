"""Check deadline expiry, retry locking and late results in an isolated schema."""

from concurrent.futures import ThreadPoolExecutor
import os
import threading
from uuid import uuid4

import psycopg2


def check():
    schema = 'provider_timeout_' + uuid4().hex
    admin = psycopg2.connect(os.environ['DATABASE_URL'])
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    previous = os.environ.get('PGOPTIONS')
    os.environ['PGOPTIONS'] = '-c search_path=' + schema
    try:
        import app as api
        import conversation_worker as worker
        from conversations import database

        client = api.app.test_client()
        with client.session_transaction() as session:
            session['auth'] = True
        def create():
            response = client.post('/api/conversations', json={
                'model': 'gemini-3.1-flash-lite', 'prompt': 'Question', 'request_id': str(uuid4()),
            })
            assert response.status_code < 300, response.json
            return response.json
        def get(cid):
            return client.get('/api/conversations/' + cid).json
        def reply():
            return dict(answer='Answer', input_tokens=1, output_tokens=1,
                        served='gemini-3.1-flash-lite', stop_reason='end_turn')

        started, release = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            started.set()
            assert release.wait(15)
            return reply()
        api.call_gemini = slow
        created = create()
        cid = created['id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            original = pool.submit(worker.run_one, api)
            assert started.wait(5)
            worker.GENERATION_SECONDS = 0
            worker.expire_slow_jobs(api)
            worker.GENERATION_SECONDS = 120
            assert get(cid)['job']['status'] == 'failed'
            assert get(cid)['job']['error_code'] == 'provider_timeout'
            assert len(get(cid)['messages']) == 1
            retried = client.post('/api/conversations/' + cid + '/retry')
            assert retried.status_code < 300, retried.json
            # Retrying while the original call is alive must not deadlock the job row.
            assert pool.submit(worker.run_one, api).result(timeout=3) is False
            fresh = create()
            api.call_gemini = lambda *args, **kwargs: reply()
            assert pool.submit(worker.run_one, api).result(timeout=3)
            assert get(fresh['id'])['job']['status'] == 'completed'
            assert get(cid)['job']['status'] == 'queued'
            release.set()
            assert original.result(timeout=5)
        assert len(get(cid)['messages']) == 1
        api.call_gemini = lambda *args, **kwargs: reply()
        assert worker.run_one(api)
        assert get(cid)['job']['status'] == 'completed'
        assert len(get(cid)['messages']) == 2

        # A result from a superseded attempt cannot complete a newer attempt.
        other = create()
        with database(api) as cur:
            cur.execute("UPDATE conversation_jobs SET status='running',started_at=now()-interval '1 second' WHERE id=%s RETURNING *", (other['job_id'],))
            stale = dict(cur.fetchone())
            cur.execute("UPDATE conversation_jobs SET started_at=now() WHERE id=%s RETURNING *", (other['job_id'],))
            current = dict(cur.fetchone())
        worker.finish(api, stale, reply())
        assert len(get(other['id'])['messages']) == 1
        worker.finish(api, current, reply())
        assert len(get(other['id'])['messages']) == 2

        # Cloud errors are persisted safely with a useful public error category.
        failed = create()
        class Rejected(Exception):
            status_code = 503
        def reject(*args, **kwargs):
            raise Rejected('private provider payload')
        api.call_gemini = reject
        assert worker.run_one(api)
        job = get(failed['id'])['job']
        assert job['error_code'] == 'provider_busy'
        assert 'private' not in job['error_detail']

        # Local AI keeps its existing four-minute limit.
        local = create()
        with database(api) as cur:
            cur.execute("UPDATE conversation_jobs SET model=%s,status='running',started_at=now()-interval '121 seconds' WHERE id=%s", (api.local_inference.MODEL_ID, local['job_id']))
        worker.expire_slow_jobs(api)
        assert get(local['id'])['job']['status'] == 'running'
        print('PASS: deadline monitor, safe errors, nonblocking retries, stale-attempt protection and local-model exemption')
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

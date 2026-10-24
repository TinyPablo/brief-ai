"""Standalone integration check using isolated Postgres tables and fake providers.

Run with PYTHONPATH=backend python backend/tests/check_conversation_api.py.
The temporary schema is removed afterwards; no real model requests are made.
"""

import base64
from concurrent.futures import ThreadPoolExecutor
import os
import threading
from uuid import uuid4

import psycopg2


def check_api():
    schema = 'conversation_api_check_' + uuid4().hex
    admin = psycopg2.connect(os.environ['DATABASE_URL'])
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    original_options = os.environ.get('PGOPTIONS')
    os.environ['PGOPTIONS'] = f'-c search_path={schema}'
    try:
        import app as api
        import conversation_worker as worker
        from conversations import database

        calls = []
        answer_started, answer_release = threading.Event(), threading.Event()
        block = False

        def fake(model, prompt, depth, images=None, messages=None, **kwargs):
            calls.append((model, messages))
            if block:
                answer_started.set()
                assert answer_release.wait(10)
            return dict(answer='B', input_tokens=10, output_tokens=5,
                        served=model, stop_reason='end_turn')

        api.call_gemini = api.call_anthropic = api.call_openai = fake

        def client():
            c = api.app.test_client()
            with c.session_transaction() as session:
                session['auth'] = True
            return c

        def post(path, **body):
            return client().post(path, json=body)

        def first(**extra):
            body = dict(prompt='A', model='gemini-3.1-flash-lite', request_id=str(uuid4()))
            body.update(extra)
            response = post('/api/conversations', **body)
            assert response.status_code == 202, response.json
            return response.json, body

        assert api.app.test_client().get('/api/conversations').status_code == 401
        assert post('/api/conversations', prompt='x').status_code == 400
        created, body = first(context='Test context', images=[{
            'media_type': 'image/png', 'data': base64.b64encode(b'fake-image').decode(),
        }])
        cid = created['id']
        assert post('/api/conversations', **body).json['id'] == cid
        state = client().get('/api/conversations/' + cid).json
        assert state['job']['status'] == 'queued'
        assert len(state['messages']) == 1
        assert worker.run_one(api)
        state = client().get('/api/conversations/' + cid).json
        assert state['job']['status'] == 'completed'
        assert len(state['messages']) == 2
        assert client().get('/api/conversation-requests/' + body['request_id']).json['id'] == cid
        assistant_id = state['messages'][1]['id']
        settings = client().get('/api/conversation-messages/' + assistant_id)
        assert settings.json['prompt_is_raw'] is True
        assert api.app.test_client().get('/api/conversation-messages/' + assistant_id).status_code == 401
        share = post('/api/conversation-messages/' + assistant_id + '/share', show_prompt=True, show_images=False)
        token = share.json['token']
        public = api.app.test_client().get('/api/share/' + token)
        assert public.status_code == 200
        assert public.json['prompt'] == 'A'
        assert public.json['answer'] == 'B'
        assert 'context' not in public.json and len(public.json['messages']) == 2
        assert len(public.json['images']) == 1
        assert len(client().get('/api/conversation-shares').json['items']) == 1
        mid = state['messages'][0]['id']
        assert state['messages'][0]['images']
        image_sha = state['messages'][0]['images'][0]['sha']
        assert api.app.test_client().get('/api/share/' + token + '/images/' + image_sha).status_code == 200
        assert client().patch('/api/conversation-messages/' + assistant_id + '/share', json={'show_images': True}).status_code == 200
        assert api.app.test_client().get('/api/share/' + token + '/images/' + image_sha).status_code == 200
        assert api.app.test_client().get('/api/share/' + token + '/images/' + '0' * 64).status_code == 404
        assert client().patch('/api/conversation-messages/' + assistant_id + '/share', json={'show_prompt': False}).status_code == 200
        assert api.app.test_client().get('/api/share/' + token).json['prompt'] is None
        assert calls[-1][1][0]['role'] == 'user'
        assert 'Test context' in calls[-1][1][0]['parts'][-1]['text']

        follow = dict(prompt='C', model='claude-haiku-4-5', request_id=str(uuid4()))
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: post(f'/api/conversations/{cid}/messages', **follow), range(2)))
        assert all(r.status_code == 202 for r in results), [r.json for r in results]
        assert post(f'/api/conversations/{cid}/messages', **dict(follow, request_id=str(uuid4()))).status_code == 409
        assert worker.run_one(api)
        assert [m['role'] for m in calls[-1][1]] == ['user', 'assistant', 'user']
        assert calls[-1][1][0]['content'][0]['type'] == 'image'
        from conversation_titles import run_title
        assert run_title(api)
        titled = client().get('/api/conversations/' + cid).json
        assert titled['title'] == 'B'
        assert titled['title_status'] == 'completed'
        assert titled['title_model'] == 'gemini-3.1-flash-lite'
        assert titled['title_input_tokens'] == 10
        assert titled['title_cost_usd'] > 0
        assert not run_title(api)
        state = client().get('/api/conversations/' + cid).json
        assert len(state['messages']) == 4
        follow_mid = state['messages'][2]['id']
        assert client().delete(f'/api/conversations/{cid}/messages/{follow_mid}').status_code == 200
        assert len(client().get('/api/conversations/' + cid).json['messages']) == 2

        # Deleting a pending turn must prevent a late provider response from restoring it.
        follow['request_id'] = str(uuid4())
        response = post(f'/api/conversations/{cid}/messages', **follow)
        block = True
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(worker.run_one, api)
            assert answer_started.wait(10)
            worker.recover_interrupted(api)
            assert client().get('/api/conversations/' + cid).json['job']['status'] == 'running'
            assert client().delete(f"/api/conversations/{cid}/messages/{response.json['message_id']}").status_code == 200
            answer_release.set()
            assert future.result()
        block = False
        assert len(client().get('/api/conversations/' + cid).json['messages']) == 2

        # Recover a job whose worker session has disappeared, then explicitly retry it.
        interrupted, _ = first()
        with database(api) as cur:
            cur.execute("UPDATE conversation_jobs SET status = 'running' WHERE id = %s", (interrupted['job_id'],))
        worker.recover_interrupted(api)
        assert client().get('/api/conversations/' + interrupted['id']).json['job']['error_code'] == 'worker_interrupted'
        assert post('/api/conversations/' + interrupted['id'] + '/retry').status_code == 202
        assert worker.run_one(api)
        assert client().get('/api/conversations/' + interrupted['id']).json['job']['status'] == 'completed'
        assert post('/api/conversations/' + interrupted['id'] + '/retry').status_code == 409

        failed, _ = first()
        def failing(*args, **kwargs):
            raise RuntimeError('simulated provider outage')
        api.call_gemini = failing
        assert worker.run_one(api)
        failed_state = client().get('/api/conversations/' + failed['id']).json
        assert failed_state['job']['status'] == 'failed'
        assert failed_state['job']['error_code'] == 'api_error'
        assert len(failed_state['messages']) == 1
        api.call_gemini = fake
        assert post('/api/conversations/' + failed['id'] + '/retry').status_code == 202
        assert worker.run_one(api)
        assert len(client().get('/api/conversations/' + failed['id']).json['messages']) == 2

        # Distinct conversations can complete concurrently without taking each other's jobs.
        parallel = [first()[0] for _ in range(2)]
        with ThreadPoolExecutor(2) as pool:
            assert all(pool.map(lambda _: worker.run_one(api), range(2)))
        for item in parallel:
            assert client().get('/api/conversations/' + item['id']).json['job']['status'] == 'completed'

        # deletion until their final reference is removed.
        with database(api) as cur:
            cur.execute("INSERT INTO prompts(model,prompt,answer) VALUES ('legacy','old','answer') RETURNING id")
            legacy_id = cur.fetchone()['id']
            cur.execute('SELECT image_sha FROM conversation_message_images WHERE message_id = %s', (mid,))
            sha = cur.fetchone()['image_sha']
            cur.execute('INSERT INTO prompt_images VALUES (%s, %s, 0)', (legacy_id, sha))
        assert client().delete(f'/api/conversations/{cid}/messages/{mid}').json['deleted_conversation']
        assert client().get('/api/conversations/' + cid).status_code == 404
        assert api.app.test_client().get('/api/share/' + token).status_code == 404
        assert client().get('/api/history/' + str(legacy_id)).status_code == 200
        assert client().get('/api/images/' + sha).status_code == 200
        assert client().delete('/api/history/' + str(legacy_id)).status_code == 200
        with database(api) as cur:
            cur.execute('SELECT count(*) AS count FROM images WHERE sha256 = %s', (sha,))
            assert cur.fetchone()['count'] == 0
        assert len(client().get('/api/conversations').json['items']) == 4
        assert client().get('/api/conversations?q=does-not-exist').json['items'] == []
        # Title failures leave the answer readable and are not retried automatically.
        api.call_gemini = failing
        assert run_title(api)
        assert client().get('/api/conversations/' + interrupted['id']).json['title_status'] == 'failed'
        from conversation_titles import recover_titles
        with database(api) as cur:
            cur.execute("UPDATE conversations SET title_status = 'running' WHERE id = %s", (interrupted['id'],))
        recover_titles(api)
        assert client().get('/api/conversations/' + interrupted['id']).json['title_status'] == 'failed'
        assert len(client().get('/api/conversations/' + interrupted['id']).json['messages']) == 2

        # Deleting during title generation must never recreate a conversation.
        title_target = failed['id']
        question_id = client().get('/api/conversations/' + title_target).json['messages'][0]['id']
        def deleting_title(*args, **kwargs):
            assert client().delete('/api/conversations/' + title_target + '/messages/' + question_id).status_code == 200
            return dict(answer='Deleted title', input_tokens=1, output_tokens=1, served='gemini-3.1-flash-lite')
        api.call_gemini = deleting_title
        assert run_title(api)
        assert client().get('/api/conversations/' + title_target).status_code == 404
        api.call_gemini = fake
        print('PASS: API auth, idempotency, concurrent sends, refresh state, provider switching, images, truncation, live worker locking, interrupted recovery and legacy history')
    finally:
        if original_options is None:
            os.environ.pop('PGOPTIONS', None)
        else:
            os.environ['PGOPTIONS'] = original_options
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


if __name__ == '__main__':
    check_api()

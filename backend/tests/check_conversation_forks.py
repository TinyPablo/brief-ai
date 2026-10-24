"""Standalone Postgres checks for independent branches and immutable public snapshots."""

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from uuid import uuid4

import psycopg2


def check_forks():
    schema = 'conversation_forks_check_' + uuid4().hex
    admin = psycopg2.connect(os.environ['DATABASE_URL'])
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    previous = os.environ.get('PGOPTIONS')
    os.environ['PGOPTIONS'] = f'-c search_path={schema}'
    try:
        import app as api
        from conversation_worker import run_one
        from conversations import database

        captured = []
        def fake(model, prompt, depth, images=None, messages=None, **kwargs):
            captured.append(messages)
            return dict(answer='Answer ' + str(len(captured)), served=model, input_tokens=10,
                        output_tokens=5, stop_reason='end_turn')
        api.call_gemini = api.call_anthropic = api.call_openai = fake
        def client(auth=True):
            c = api.app.test_client()
            if auth:
                with c.session_transaction() as session:
                    session['auth'] = True
            return c
        def post(path, body=None):
            return client().post(path, json=body or {})
        def get(cid):
            res = client().get('/api/conversations/' + cid)
            assert res.status_code == 200, res.json
            return res.json
        def upload(text):
            return {'media_type': 'image/png', 'data': base64.b64encode(text.encode()).decode()}
        first = post('/api/conversations', dict(prompt='A', model='gemini-3.1-flash-lite',
                     context='PRIVATE SETTINGS CONTEXT', images=[upload('image A')], request_id=str(uuid4())))
        assert first.status_code == 202, first.json
        cid = first.json['id']
        assert run_one(api)
        post('/api/conversations/' + cid + '/messages', dict(prompt='C', model='claude-haiku-4-5', images=[upload('image C')], request_id=str(uuid4())))
        assert run_one(api)
        original = get(cid)
        a, b, c, d = original['messages']
        assert len(original['messages']) == 4
        early = post('/api/conversation-messages/' + b['id'] + '/share')
        token_b = early.json['token']
        shared = post('/api/conversation-messages/' + d['id'] + '/share')
        token_d = shared.json['token']
        public_client = client(False)
        def public(token):
            response = public_client.get('/api/share/' + token)
            assert response.status_code == 200, response.json
            return response.json
        snapshot = public(token_d)
        assert snapshot['scope'] == 'conversation'
        assert [m['content'] for m in snapshot['messages']] == ['A', 'Answer 1', 'C', 'Answer 2']
        assert len(snapshot['messages'][0]['images']) == len(snapshot['messages'][2]['images']) == 1
        assert 'PRIVATE SETTINGS CONTEXT' not in json.dumps(snapshot)
        assert 'input_tokens' not in json.dumps(snapshot) and 'cost_usd' not in json.dumps(snapshot)
        sha_a, sha_c = a['images'][0]['sha'], c['images'][0]['sha']
        assert public_client.get('/api/share/' + token_d + '/images/' + sha_a).status_code == 200
        assert public_client.get('/api/share/' + token_d + '/images/' + sha_c).status_code == 200
        assert public_client.get('/api/share/' + token_b + '/images/' + sha_c).status_code == 404
        assert client(False).post('/api/conversations/' + cid + '/branches', json={}).status_code == 401
        request_id = str(uuid4())
        def fork(_):
            return post('/api/conversations/' + cid + '/branches', dict(message_id=b['id'], request_id=request_id))
        with ThreadPoolExecutor(2) as pool:
            copies = list(pool.map(fork, range(2)))
        assert all(response.status_code == 201 for response in copies), [r.json for r in copies]
        fork_b = copies[0].json['id']
        assert copies[1].json['id'] == fork_b
        assert post('/api/conversations/' + cid + '/branches', dict(message_id=d['id'], request_id=request_id)).status_code == 409
        copy = get(fork_b)
        assert copy['context'] == original['context']
        assert len(copy['messages']) == 2
        assert copy['messages'][0]['id'] != a['id']
        assert copy['messages'][0]['created_at'] == a['created_at']
        assert copy['messages'][0]['images'][0]['sha'] == sha_a
        assert all(m['inherited'] for m in copy['messages'])
        assert post('/api/conversations/' + cid + '/branches', dict(message_id=a['id'], request_id=str(uuid4()))).status_code == 404
        full_fork = post('/api/conversations/' + cid + '/branches', dict(message_id=d['id'], request_id=str(uuid4())))
        fork_d = full_fork.json['id']
        assert len(get(fork_d)['messages']) == 4
        copy_b_answer = copy['messages'][1]['id']
        fork_token = post('/api/conversation-messages/' + copy_b_answer + '/share').json['token']
        assert public(fork_token)['messages'][0]['images'][0]['sha'] == sha_a
        # All scopes are frozen; attachments follow the included messages.
        for scope, count in [('answer', 1), ('exchange', 2), ('conversation', 4)]:
            changed = client().patch('/api/conversation-messages/' + d['id'] + '/share', json={'scope': scope})
            assert changed.status_code == 200
            assert changed.json['token'] == token_d
            scoped = public(token_d)
            assert len(scoped['messages']) == count
            assert public_client.get('/api/share/' + token_d + '/images/' + sha_a).status_code == (200 if scope == 'conversation' else 404)
        frozen = public(token_d)
        with database(api) as cur:
            cur.execute("UPDATE conversation_messages SET content = 'edited outside API' WHERE id = %s", (b['id'],))
        assert public(token_d) == frozen
        # A later pending turn is excluded from both snapshots and earlier forks.
        later = post('/api/conversations/' + cid + '/messages', dict(prompt='E', model='gemini-3.1-flash-lite', request_id=str(uuid4())))
        assert later.status_code == 202
        assert public(token_d) == frozen
        pending_fork = post('/api/conversations/' + cid + '/branches', dict(message_id=d['id'], request_id=str(uuid4())))
        assert len(get(pending_fork.json['id'])['messages']) == 4
        assert client().delete('/api/conversations/' + cid + '/messages/' + later.json['message_id']).status_code == 200
        assert public(token_d) == frozen
        # Removing C revokes the later snapshot but leaves AB's link and every fork.
        assert client().delete('/api/conversations/' + cid + '/messages/' + c['id']).status_code == 200
        assert public_client.get('/api/share/' + token_d).status_code == 404
        assert len(public(token_b)['messages']) == 2
        assert len(get(fork_d)['messages']) == 4
        assert len(get(fork_b)['messages']) == 2
        assert client().delete('/api/conversations/' + cid + '/messages/' + a['id']).status_code == 200
        assert public_client.get('/api/share/' + token_b).status_code == 404
        assert len(public(fork_token)['messages']) == 2
        assert public_client.get('/api/share/' + fork_token + '/images/' + sha_a).status_code == 200
        # Continue the copy after the original conversation no longer exists.
        follow = post('/api/conversations/' + fork_b + '/messages', dict(prompt='Branch follow-up', model='claude-haiku-4-5', request_id=str(uuid4())))
        assert follow.status_code == 202
        assert run_one(api)
        assert len(get(fork_b)['messages']) == 4
        assert not get(fork_b)['messages'][-1]['inherited']
        assert [m['role'] for m in captured[-1]] == ['user', 'assistant', 'user']
        assert captured[-1][0]['content'][0]['source']['data'] == base64.b64encode(b'image A').decode()
        assert len(public(fork_token)['messages']) == 2
        # Direct deletion of any included source also revokes the entire snapshot.
        with database(api) as cur:
            cur.execute('DELETE FROM conversation_messages WHERE id = %s', (copy['messages'][0]['id'],))
        assert public_client.get('/api/share/' + fork_token).status_code == 404
        assert public_client.get('/api/share/' + fork_token + '/images/' + sha_a).status_code == 404
        with database(api) as cur:
            cur.execute('SELECT count(*) AS count FROM images')
            assert cur.fetchone()['count'] == 2, 'Forks duplicated or lost image bytes'
        print('PASS: independent/idempotent forks, copied attachments, all share scopes, immutable snapshots, private context exclusion, image authorization, pending-turn exclusion, scoped revocation and continued forks after original deletion')
    finally:
        if previous is None:
            os.environ.pop('PGOPTIONS', None)
        else:
            os.environ['PGOPTIONS'] = previous
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


if __name__ == '__main__':
    check_forks()

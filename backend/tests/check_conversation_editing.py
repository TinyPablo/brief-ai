"""Postgres checks for editing, truncation and explicit answer generation."""

import base64
import os
from uuid import uuid4

import psycopg2


def check_editing():
    schema = 'conversation_edit_check_' + uuid4().hex
    admin = psycopg2.connect(os.environ['DATABASE_URL'])
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    previous = os.environ.get('PGOPTIONS')
    os.environ['PGOPTIONS'] = f'-c search_path={schema}'
    try:
        import app as api
        from conversations import database
        from conversation_worker import finish, run_one

        calls = []
        def fake(model, prompt, depth, images=None, messages=None, **kwargs):
            calls.append(messages)
            return dict(answer='Generated answer', served=model, input_tokens=10,
                        output_tokens=5, stop_reason='end_turn')
        api.call_gemini = api.call_anthropic = api.call_openai = fake
        client = api.app.test_client()
        with client.session_transaction() as session:
            session['auth'] = True
        def post(path, **body):
            if path.endswith('/generate'):
                body.setdefault('message_id', get(path.split('/')[2])['messages'][-1]['id'])
            return client.post('/api' + path, json=body)
        def get(cid):
            return client.get('/api/conversations/' + cid).json
        def edit(cid, mid, text, revision=None):
            return client.patch('/api/conversations/' + cid + '/messages/' + mid,
                                json=dict(content=text, revision=get(cid)['revision'] if revision is None else revision))
        model = 'gemini-3.1-flash-lite'
        created = post('/conversations', prompt='A', model=model, request_id=str(uuid4()),
                       images=[dict(media_type='image/png', data=base64.b64encode(b'attachment').decode())])
        cid = created.json['id']
        assert run_one(api)
        post('/conversations/' + cid + '/messages', prompt='C', model=model, request_id=str(uuid4()))
        assert run_one(api)
        a, b, c, d = get(cid)['messages']
        early_token = post('/conversation-messages/' + b['id'] + '/share').json['token']
        late_token = post('/conversation-messages/' + d['id'] + '/share').json['token']
        fork = post('/conversations/' + cid + '/branches', message_id=d['id'], request_id=str(uuid4())).json['id']
        path = '/api/conversations/' + cid + '/messages/' + c['id']
        assert api.app.test_client().patch(path, json={}).status_code == 401
        for body in ({}, {'content': ''}, {'content': 'x', 'revision': True}, {'content': 'x' * 100001, 'revision': 0}):
            assert client.patch(path, json=body).status_code == 400
        assert edit(cid, str(uuid4()), 'missing').status_code == 404
        assert edit(cid, c['id'], 'Edited C').status_code == 200
        edited = get(cid)
        assert [m['content'] for m in edited['messages']] == ['A', 'Generated answer', 'Edited C']
        assert edited['messages'][-1]['edited_at'] and edited['job']['status'] == 'completed'
        assert len(calls) == 2
        assert edit(cid, c['id'], 'Stale edit', revision=0).status_code == 409
        assert client.get('/api/share/' + early_token).status_code == 200
        assert client.get('/api/share/' + late_token).status_code == 404
        assert len(get(fork)['messages']) == 4
        request_id = str(uuid4())
        for _ in range(2):
            assert post('/conversations/' + cid + '/generate', request_id=request_id, model=model).status_code == 202
        assert post('/conversations/' + cid + '/generate', request_id=str(uuid4()), model=model).status_code == 409
        assert run_one(api)
        assert calls[-1][-1]['parts'][-1]['text'] == 'Edited C'
        # Editing an assistant response keeps it as the final turn and allows a follow-up.
        assert edit(cid, b['id'], 'Manually corrected B').status_code == 200
        assert [m['content'] for m in get(cid)['messages']] == ['A', 'Manually corrected B']
        assert client.get('/api/share/' + early_token).status_code == 404
        assert post('/conversations/' + cid + '/generate', request_id=str(uuid4()), model=model).status_code == 409
        assert post('/conversations/' + cid + '/messages', prompt='Next question', model=model, request_id=str(uuid4())).status_code == 202
        # A model response finishing after the edit must not restore the old tail.
        with database(api) as cur:
            cur.execute("UPDATE conversation_jobs SET status = 'running' WHERE conversation_id = %s AND status = 'queued' RETURNING *", (cid,))
            old_job = dict(cur.fetchone())
        latest = get(cid)['messages'][-1]
        assert edit(cid, latest['id'], 'Edited while running').status_code == 200
        finish(api, old_job, fake(model, '', '', messages=[]))
        assert get(cid)['messages'][-1]['content'] == 'Edited while running'
        assert len(get(cid)['messages']) == 3
        assert get(cid)['job']['status'] == 'completed'
        assert edit(cid, a['id'], 'Edited first question').status_code == 200
        final = get(cid)
        assert len(final['messages']) == 1 and final['job'] is None
        assert final['messages'][0]['images'] == a['images']
        assert len(get(fork)['messages']) == 4
        assert post('/conversations/' + cid + '/generate', request_id=str(uuid4()), model=model).status_code == 202
        assert run_one(api)
        assert len(get(cid)['messages']) == 2
        last_answer = get(cid)['messages'][-1]
        assert edit(cid, last_answer['id'], last_answer['content']).status_code == 200
        copied = post('/conversations/' + cid + '/branches', message_id=last_answer['id'], request_id=str(uuid4())).json['id']
        assert get(copied)['messages'][-1]['edited_at']
        new_token = post('/conversation-messages/' + last_answer['id'] + '/share').json['token']
        assert edit(cid, last_answer['id'], 'New manual answer').status_code == 200
        assert client.get('/api/share/' + new_token).status_code == 404
        republished = post('/conversation-messages/' + last_answer['id'] + '/share').json['token']
        assert republished != new_token
        assert client.get('/api/share/' + republished).json['answer'] == 'New manual answer'
        assert edit(cid, last_answer['id'], '    indented code\n').status_code == 200
        assert get(cid)['messages'][-1]['content'] == '    indented code\n'
        print('PASS: user/assistant edits, tail deletion, attachments, scoped share revocation, independent branches, stale edits, explicit/idempotent generation and stale worker completion')
    finally:
        if previous is None:
            os.environ.pop('PGOPTIONS', None)
        else:
            os.environ['PGOPTIONS'] = previous
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


if __name__ == '__main__':
    check_editing()

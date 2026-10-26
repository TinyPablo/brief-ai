"""Exercise chart persistence and snapshot lifecycle in an isolated Postgres schema."""

import json
import os
from uuid import uuid4

import psycopg2


def check_storage():
    schema = 'chart_check_' + uuid4().hex
    admin = psycopg2.connect(os.environ['DATABASE_URL'])
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    previous = os.environ.get('PGOPTIONS')
    os.environ['PGOPTIONS'] = '-c search_path=' + schema
    try:
        import app as api
        from conversation_worker import run_one
        chart = dict(version=1, type='bar', title='Expenses', x_label='Month', y_label='PLN',
                     source='conversation', assumptions='',
                     series=[dict(name='Expenses', points=[dict(x='January', y=1200)])])
        calls = []
        def fake(model, prompt, depth, **kwargs):
            assert kwargs['chart_tools'] is True
            calls.append(kwargs['messages'])
            return dict(answer='Comparison.', charts=[chart], served=model, input_tokens=10,
                        output_tokens=5, stop_reason='end_turn', cost_usd=.123)
        api.call_anthropic = api.call_gemini = api.call_openai = fake
        client = api.app.test_client()
        with client.session_transaction() as session:
            session['auth'] = True
        public = api.app.test_client()
        def post(path, **body):
            response = client.post('/api'+path, json=body)
            assert response.status_code < 300, response.json
            return response.json
        def get(cid):
            return client.get('/api/conversations/'+cid).json
        created = post('/conversations', prompt='Compare spending', model='gemini-3.1-flash-lite',
                       request_id=str(uuid4()))
        cid = created['id']
        assert run_one(api)
        answer = get(cid)['messages'][-1]
        assert answer['charts'] == [chart] and answer['cost_usd'] == .123
        for scope in ('answer', 'exchange', 'conversation'):
            shared = post('/conversation-messages/'+answer['id']+'/share', scope=scope)
            response = public.get('/api/share/'+shared['token'])
            assert response.status_code == 200
            assert response.json['charts'] == [chart]
            assert response.json['messages'][-1]['charts'] == [chart]
        token = shared['token']
        copied = post('/conversations/'+cid+'/branches', message_id=answer['id'], request_id=str(uuid4()))['id']
        assert get(copied)['messages'][-1]['charts'] == [chart]
        post('/conversations/'+cid+'/messages', prompt='Explain January', model='gpt-5.4-nano', request_id=str(uuid4()))
        assert run_one(api)
        assert '1200' in json.dumps(calls[-1])
        assert len(public.get('/api/share/'+token).json['messages']) == 2
        edited = client.patch('/api/conversations/'+cid+'/messages/'+answer['id'],
                              json=dict(content='Corrected explanation.', revision=get(cid)['revision'], request_id=str(uuid4())))
        assert edited.status_code == 200, edited.json
        assert get(cid)['messages'][-1]['charts'] == [chart]
        assert public.get('/api/share/'+token).status_code == 404
        assert get(copied)['messages'][-1]['charts'] == [chart]
        first_id = get(cid)['messages'][0]['id']
        assert client.delete('/api/conversations/'+cid+'/messages/'+first_id).status_code == 200
        assert get(copied)['messages'][-1]['charts'] == [chart]
        print('PASS: chart persistence, all share scopes, snapshot immutability, branches, model-switch context, edit revocation and independent deletion')
    finally:
        if previous is None:
            os.environ.pop('PGOPTIONS', None)
        else:
            os.environ['PGOPTIONS'] = previous
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


if __name__ == '__main__':
    check_storage()

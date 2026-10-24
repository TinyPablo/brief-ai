"""Run durable conversation jobs independently of HTTP requests."""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from conversation_content import provider_messages
from conversations import database

log = logging.getLogger(__name__)
LOCK_NAMESPACE = 477


def load_turns(api, cur, job):
    cur.execute('SELECT context FROM conversations WHERE id = %s', (str(job['conversation_id']),))
    conversation = cur.fetchone()
    cur.execute('SELECT * FROM conversation_messages WHERE conversation_id = %s ORDER BY position',
                (str(job['conversation_id']),))
    turns = [dict(row) for row in cur.fetchall()]
    if not conversation or not turns:
        return []
    cur.execute(
        '''SELECT mi.message_id, i.media_type, i.bytes FROM conversation_message_images mi
           JOIN images i ON i.sha256 = mi.image_sha JOIN conversation_messages m ON m.id = mi.message_id
           WHERE m.conversation_id = %s ORDER BY m.position, mi.position''',
        (str(job['conversation_id']),),
    )
    by_message = {}
    for row in cur.fetchall():
        by_message.setdefault(str(row['message_id']), []).append({
            'media_type': row['media_type'], 'raw': bytes(row['bytes']),
        })
    for i, turn in enumerate(turns):
        turn['images'] = by_message.get(str(turn['id']), [])
        if turn['role'] == 'user':
            turn['content'] = api.compose_prompt(
                turn['content'], conversation['context'] if i == 0 else '', turn['brief'],
            )
    return turns


def generate(api, job, turns):
    model = job['model']
    provider = api.MODELS[model]['provider']
    messages = provider_messages(turns, provider)
    if provider == 'local':
        response = api.local_inference.generate([
            {'role': 'system', 'content': api.SYSTEM_PROMPT}, *messages,
        ])
        choice = response['choices'][0]
        return dict(answer=choice['message']['content'] or '_No response._',
                    input_tokens=response['usage']['prompt_tokens'],
                    output_tokens=response['usage']['completion_tokens'],
                    stop_reason=choice['finish_reason'], served=model)
    call = {'anthropic': api.call_anthropic, 'google': api.call_gemini,
            'openai': api.call_openai}[provider]
    return call(model, '', api.DEFAULT_DEPTH, messages=messages)


def recover_interrupted(api):
    # Session locks disappear on worker death. Live requests never get retried
    # automatically because a provider may already have charged for the call.
    with database(api) as cur:
        cur.execute("SELECT id FROM conversation_jobs WHERE status = 'running'")
        for row in cur.fetchall():
            jid = str(row['id'])
            cur.execute('SELECT pg_try_advisory_xact_lock(%s, hashtext(%s)) AS acquired',
                        (LOCK_NAMESPACE, jid))
            if cur.fetchone()['acquired']:
                cur.execute(
                    """UPDATE conversation_jobs SET status = 'failed', finished_at = now(),
                       error_code = 'worker_interrupted',
                       error_detail = 'Generation was interrupted. Retry the message to continue.'
                       WHERE id = %s AND status = 'running'""", (jid,),
                )


def finish(api, job, result=None, duration_ms=0, error=None):
    with database(api) as cur:
        cur.execute('SELECT revision FROM conversations WHERE id = %s FOR UPDATE',
                    (str(job['conversation_id']),))
        conversation = cur.fetchone()
        if not conversation or conversation['revision'] != job['revision']:
            return
        cur.execute('SELECT status FROM conversation_jobs WHERE id = %s FOR UPDATE', (str(job['id']),))
        state = cur.fetchone()
        if not state or state['status'] != 'running':
            return
        if error:
            cur.execute("""UPDATE conversation_jobs SET status = 'failed', finished_at = now(),
                        error_code = %s, error_detail = %s WHERE id = %s""",
                        (error[0], error[1], str(job['id'])))
            return
        served = result['served']
        pricing = api.MODELS.get(served, api.MODELS[job['model']])
        cost = (result['input_tokens'] * pricing['input'] + result['output_tokens'] * pricing['output']) / 1_000_000
        cur.execute('SELECT position FROM conversation_messages WHERE id = %s', (str(job['user_message_id']),))
        message = cur.fetchone()
        if not message:
            return
        cur.execute(
            """INSERT INTO conversation_messages
               (id, conversation_id, position, role, content, model, input_tokens, output_tokens,
                cost_usd, duration_ms, stop_reason, reasoning)
               VALUES (%s, %s, %s, 'assistant', %s, %s, %s, %s, %s, %s, %s, %s)""",
            (str(uuid4()), str(job['conversation_id']), message['position'] + 1,
             result['answer'], served, result['input_tokens'], result['output_tokens'],
             cost, duration_ms, result['stop_reason'], api.build_reasoning_label(job['model'], api.DEFAULT_DEPTH)),
        )
        cur.execute("UPDATE conversation_jobs SET status = 'completed', finished_at = now() WHERE id = %s", (str(job['id']),))
        cur.execute('UPDATE conversations SET updated_at = now() WHERE id = %s', (str(job['conversation_id']),))


def run_one(api):
    conn = api.get_db()
    try:
        with conn, conn.cursor(cursor_factory=api.psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM conversation_jobs WHERE status = 'queued' ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED")
            job = cur.fetchone()
            if not job:
                return False
            cur.execute('SELECT pg_advisory_lock(%s, hashtext(%s))', (LOCK_NAMESPACE, str(job['id'])))
            cur.execute("UPDATE conversation_jobs SET status = 'running', started_at = now() WHERE id = %s", (str(job['id']),))
        started = time.perf_counter()
        try:
            with database(api) as cur:
                cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                turns = load_turns(api, cur, job)
            if turns:
                result = generate(api, job, turns)
                finish(api, job, result, int((time.perf_counter() - started) * 1000))
        except Exception as exc:
            log.exception('Conversation job failed: %s', job['id'])
            code = exc.code if isinstance(exc, api.local_inference.LocalError) else 'api_error'
            detail = exc.detail if isinstance(exc, api.local_inference.LocalError) else 'The model request failed. Retry the message to continue.'
            finish(api, job, error=(code, detail))
        return True
    finally:
        conn.close()


def work(api):
    while True:
        try:
            recover_interrupted(api)
            if not run_one(api):
                time.sleep(1)
        except Exception:
            log.exception('Conversation worker iteration failed')
            time.sleep(2)


if __name__ == '__main__':
    import app as api

    logging.basicConfig(level=logging.INFO)
    concurrency = max(1, min(8, int(os.environ.get('CONVERSATION_WORKERS', '4'))))
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(work, api) for _ in range(concurrency)]
        for future in futures:
            future.result()

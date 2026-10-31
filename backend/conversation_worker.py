"""Run durable conversation jobs independently of HTTP requests."""

import logging
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from psycopg2.extras import Json

from charts import validate_charts

from conversation_content import provider_messages
from conversations import database
from provider_requests import GENERATION_SECONDS, describe_error
from conversation_events import Listener, WORK_CHANNEL

log = logging.getLogger(__name__)
LOCK_NAMESPACE = 477


def load_turns(api, cur, job):
    cur.execute('SELECT context, revision FROM conversations WHERE id = %s', (str(job['conversation_id']),))
    conversation = cur.fetchone()
    cur.execute('SELECT * FROM conversation_messages WHERE conversation_id = %s ORDER BY position',
                (str(job['conversation_id']),))
    turns = [dict(row) for row in cur.fetchall()]
    if not conversation or conversation['revision'] != job['revision'] or not turns:
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
        from chart_generation import LOCAL_CHART_INSTRUCTIONS, extract_local_charts
        response = api.local_inference.generate([
            {'role': 'system', 'content': api.SYSTEM_PROMPT + '\n\n' + LOCAL_CHART_INSTRUCTIONS}, *messages,
        ])
        choice = response['choices'][0]
        answer, charts = extract_local_charts(choice['message']['content'] or '')
        return dict(answer=answer, charts=charts,
                    input_tokens=response['usage']['prompt_tokens'],
                    output_tokens=response['usage']['completion_tokens'],
                    stop_reason=choice['finish_reason'], served=model)
    call = {'anthropic': api.call_anthropic, 'google': api.call_gemini,
            'openai': api.call_openai}[provider]
    return call(model, '', api.DEFAULT_DEPTH, messages=messages, chart_tools=True)


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


def expire_slow_jobs(api):
    models = [model for model, config in api.MODELS.items() if config['provider'] != 'local']
    error = describe_error(TimeoutError())
    with database(api) as cur:
        cur.execute(
            """UPDATE conversation_jobs SET status = 'failed', finished_at = now(),
               error_code = %s, error_detail = %s
               WHERE status = 'running' AND model = ANY(%s)
               AND started_at <= now() - (%s * interval '1 second')""",
            (error.code, error.detail, models, GENERATION_SECONDS),
        )


def monitor_deadlines(api):
    while True:
        try:
            expire_slow_jobs(api)
        except Exception:
            log.exception('Generation deadline monitor failed')
        time.sleep(1)


def finish(api, job, result=None, duration_ms=0, error=None):
    charts = validate_charts(result.get('charts', [])) if result is not None else []
    with database(api) as cur:
        cur.execute('SELECT revision FROM conversations WHERE id = %s FOR UPDATE',
                    (str(job['conversation_id']),))
        conversation = cur.fetchone()
        if not conversation or conversation['revision'] != job['revision']:
            return
        cur.execute('SELECT status, started_at FROM conversation_jobs WHERE id = %s FOR UPDATE', (str(job['id']),))
        state = cur.fetchone()
        if not state or state['status'] != 'running' or state['started_at'] != job['started_at']:
            return
        if error:
            cur.execute("""UPDATE conversation_jobs SET status = 'failed', finished_at = now(),
                        error_code = %s, error_detail = %s WHERE id = %s""",
                        (error[0], error[1], str(job['id'])))
            return
        served = result['served']
        pricing = api.MODELS.get(served, api.MODELS[job['model']])
        cost = result.get('cost_usd', (result['input_tokens'] * pricing['input'] + result['output_tokens'] * pricing['output']) / 1_000_000)
        cur.execute('SELECT position FROM conversation_messages WHERE id = %s', (str(job['user_message_id']),))
        message = cur.fetchone()
        if not message:
            return
        cur.execute(
            """INSERT INTO conversation_messages
               (id, conversation_id, position, role, content, model, input_tokens, output_tokens,
                cost_usd, duration_ms, stop_reason, reasoning, charts)
               VALUES (%s, %s, %s, 'assistant', %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (str(uuid4()), str(job['conversation_id']), message['position'] + 1,
             result['answer'], served, result['input_tokens'], result['output_tokens'],
             cost, duration_ms, result['stop_reason'], api.build_reasoning_label(job['model'], api.DEFAULT_DEPTH), Json(charts)),
        )
        cur.execute("UPDATE conversation_jobs SET status = 'completed', finished_at = now() WHERE id = %s", (str(job['id']),))
        cur.execute(
            """UPDATE conversations SET updated_at = now(),
               title_status = CASE WHEN title IS NULL AND title_status = 'waiting'
                   THEN 'queued' ELSE title_status END,
               title_model = COALESCE(title_model, %s) WHERE id = %s""",
            (job['model'], str(job['conversation_id'])),
        )


def run_one(api):
    conn = api.get_db()
    try:
        with conn, conn.cursor(cursor_factory=api.psycopg2.extras.RealDictCursor) as cur:
            skipped = []
            while True:
                cur.execute("""SELECT * FROM conversation_jobs WHERE status = 'queued'
                            AND NOT (id = ANY(%s::uuid[]))
                            ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED""", (skipped,))
                job = cur.fetchone()
                if not job:
                    return False
                # Expired attempts can still own a session lock. Do not block their
                # completion transaction or starve unrelated queued conversations.
                cur.execute('SELECT pg_try_advisory_lock(%s, hashtext(%s)) AS acquired', (LOCK_NAMESPACE, str(job['id'])))
                if cur.fetchone()['acquired']:
                    break
                skipped.append(str(job['id']))
            cur.execute("UPDATE conversation_jobs SET status = 'running', started_at = now() WHERE id = %s RETURNING started_at", (str(job['id']),))
            job['started_at'] = cur.fetchone()['started_at']
        started = time.perf_counter()
        try:
            with database(api) as cur:
                cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                turns = load_turns(api, cur, job)
            if turns:
                result = generate(api, job, turns)
                finish(api, job, result, int((time.perf_counter() - started) * 1000))
        except Exception as exc:
            error = exc if isinstance(exc, api.local_inference.LocalError) else describe_error(exc)
            log.warning('Conversation job failed: job=%s model=%s code=%s exception=%s',
                        job['id'], job['model'], error.code, type(exc).__name__)
            finish(api, job, error=(error.code, error.detail))
        return True
    finally:
        conn.close()


def work(api, stop=None):
    stop = stop or threading.Event()
    listener = None
    while not stop.is_set():
        try:
            # LISTEN must commit before checking the queue to avoid a lost wakeup.
            if listener is None:
                listener = Listener(api, WORK_CHANNEL)
            recover_interrupted(api)
            from conversation_titles import recover_titles, run_title
            recover_titles(api)
            if not run_one(api) and not run_title(api):
                listener.wait(timeout=5)
        except Exception:
            log.exception('Conversation worker iteration failed')
            if listener is not None:
                listener.close()
                listener = None
            time.sleep(2)
    if listener is not None:
        listener.close()


if __name__ == '__main__':
    import app as api

    logging.basicConfig(level=logging.INFO)
    # Keep the UI deadline enforceable even if all provider threads are blocked.
    threading.Thread(target=monitor_deadlines, args=(api,), daemon=True).start()
    concurrency = max(1, min(8, int(os.environ.get('CONVERSATION_WORKERS', '4'))))
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(work, api) for _ in range(concurrency)]
        for future in futures:
            future.result()

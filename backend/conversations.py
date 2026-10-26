"""Authenticated conversation API backed by a durable Postgres queue."""

from contextlib import contextmanager
from uuid import UUID, uuid4

import psycopg2.extras
from flask import jsonify, request

MAX_MESSAGE_CHARS = 100_000
MAX_CONVERSATION_CHARS = 500_000
MAX_CONVERSATION_MESSAGES = 100


@contextmanager
def database(api):
    conn = api.get_db()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
    finally:
        conn.close()


def json_row(row):
    return {key: value.isoformat() if hasattr(value, 'isoformat') else str(value)
            if isinstance(value, UUID) else value for key, value in dict(row).items()}


def image_metadata(cur, messages):
    if not messages:
        return
    cur.execute(
        """SELECT mi.message_id, i.sha256 AS sha, i.media_type, i.byte_size,
                  i.width, i.height, (i.thumb IS NOT NULL) AS has_thumb
           FROM conversation_message_images mi JOIN images i ON i.sha256 = mi.image_sha
           WHERE mi.message_id = ANY(%s::uuid[]) ORDER BY mi.position""",
        ([str(m['id']) for m in messages],),
    )
    by_message = {}
    for image in cur.fetchall():
        image = json_row(image)
        by_message.setdefault(str(image.pop('message_id')), []).append(image)
    for message in messages:
        message['images'] = by_message.get(str(message['id']), [])


def register_routes(app, api):
    def validate_body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return None, (jsonify(error='invalid_request'), 400)
        if not isinstance(data.get('prompt'), str):
            return None, (jsonify(error='empty_prompt'), 400)
        if len(data['prompt']) > MAX_MESSAGE_CHARS:
            return None, (jsonify(error='prompt_too_long'), 400)
        model = data.get('model', api.DEFAULT_MODEL)
        if not isinstance(model, str) or model not in api.SELECTABLE_MODELS:
            return None, (jsonify(error='unknown_model'), 400)
        context = data.get('context', '')
        if not isinstance(context, str) or len(context) > api.MAX_CONTEXT_CHARS:
            return None, (jsonify(error='context_too_long'), 400)
        if type(data.get('brief', False)) is not bool:
            return None, (jsonify(error='invalid_brief'), 400)
        try:
            request_id = str(UUID(data['request_id']))
        except (KeyError, ValueError, TypeError, AttributeError):
            return None, (jsonify(error='invalid_request_id'), 400)
        error, images = api.validate_images(data.get('images'))
        if error:
            return None, (jsonify(error=error[0], detail=error[1]), 400)
        if not data['prompt'].strip() and not images:
            return None, (jsonify(error='empty_prompt'), 400)
        if images and not api.MODELS[model].get('supports_images', True):
            return None, (jsonify(error='images_not_supported'), 400)
        return dict(prompt=data['prompt'].strip(), model=model, context=context.strip(),
                    brief=data.get('brief', False), request_id=request_id, images=images), None

    def enqueue(cur, cid, body, revision, position):
        mid = str(uuid4())
        cur.execute(
            """INSERT INTO conversation_messages
               (id, conversation_id, position, role, content, model, brief)
               VALUES (%s, %s, %s, 'user', %s, %s, %s) RETURNING created_at""",
            (mid, cid, position, body['prompt'], body['model'], body['brief']),
        )
        created = cur.fetchone()['created_at'].isoformat()
        api.store_images(cur, mid, body['images'], conversation=True)
        cur.execute(
            """INSERT INTO conversation_jobs
               (id, conversation_id, user_message_id, model, revision)
               VALUES (%s, %s, %s, %s, %s)""",
            (body['request_id'], cid, mid, body['model'], revision),
        )
        cur.execute('UPDATE conversations SET updated_at = now() WHERE id = %s', (cid,))
        return jsonify(id=cid, job_id=body['request_id'], message_id=mid, created_at=created), 202

    def existing_request(cur, request_id, cid=None):
        # Serialize retries even before the conversation exists.
        cur.execute('SELECT pg_advisory_xact_lock(478, hashtext(%s))', (request_id,))
        cur.execute('SELECT conversation_id, user_message_id FROM conversation_jobs WHERE id = %s',
                    (request_id,))
        row = cur.fetchone()
        if row:
            if cid is not None and str(row['conversation_id']) != str(cid):
                return jsonify(error='request_id_conflict'), 409
            return jsonify(id=str(row['conversation_id']), job_id=request_id,
                           message_id=str(row['user_message_id'])), 202
        return None

    @app.post('/api/conversations')
    @api.require_auth
    def create_conversation():
        body, error = validate_body()
        if error:
            return error
        with database(api) as cur:
            previous = existing_request(cur, body['request_id'])
            if previous is not None:
                return previous
            cid = str(uuid4())
            cur.execute('INSERT INTO conversations(id, context) VALUES (%s, %s)',
                        (cid, body['context'] or None))
            return enqueue(cur, cid, body, 0, 0)

    @app.get('/api/conversation-requests/<uuid:request_id>')
    @api.require_auth
    def get_request(request_id):
        with database(api) as cur:
            cur.execute('SELECT conversation_id FROM conversation_jobs WHERE id = %s', (str(request_id),))
            row = cur.fetchone()
            if not row:
                return jsonify(error='not_found'), 404
            return jsonify(id=str(row['conversation_id']), job_id=str(request_id))

    @app.get('/api/conversations')
    @api.require_auth
    def list_conversations():
        query = (request.args.get('q') or '').strip()
        before = request.args.get('before')
        try:
            before = str(UUID(before)) if before else None
        except ValueError:
            return jsonify(error='invalid_cursor'), 400
        with database(api) as cur:
            cur.execute(
                """SELECT c.*, (SELECT LEFT(content, 140) FROM conversation_messages
                       WHERE conversation_id = c.id ORDER BY position LIMIT 1) AS prompt_preview,
                       (SELECT count(*) FROM conversation_messages WHERE conversation_id = c.id) AS message_count,
                       (SELECT status FROM conversation_jobs WHERE conversation_id = c.id
                        ORDER BY created_at DESC LIMIT 1) AS status
                   FROM conversations c
                   WHERE (%s IS NULL OR (c.created_at, c.id) <
                       (SELECT created_at, id FROM conversations WHERE id = %s))
                   AND (%s = '' OR c.title ILIKE %s OR EXISTS (
                       SELECT 1 FROM conversation_messages m WHERE m.conversation_id = c.id
                       AND m.content ILIKE %s))
                   ORDER BY c.created_at DESC, c.id DESC LIMIT 30""",
                (before, before, query, '%' + query + '%', '%' + query + '%'),
            )
            return jsonify(items=[json_row(r) for r in cur.fetchall()])

    @app.get('/api/conversations/<uuid:cid>')
    @api.require_auth
    def get_conversation(cid):
        with database(api) as cur:
            # A single snapshot prevents polling between message insertion and job completion.
            cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            cur.execute('SELECT * FROM conversations WHERE id = %s', (str(cid),))
            row = cur.fetchone()
            if not row:
                return jsonify(error='not_found'), 404
            result = json_row(row)
            cur.execute('SELECT * FROM conversation_messages WHERE conversation_id = %s ORDER BY position',
                        (str(cid),))
            messages = [json_row(m) for m in cur.fetchall()]
            image_metadata(cur, messages)
            cur.execute('SELECT * FROM conversation_jobs WHERE conversation_id = %s ORDER BY created_at DESC LIMIT 1',
                        (str(cid),))
            job = cur.fetchone()
            result.update(messages=messages, job=json_row(job) if job else None)
            return jsonify(result)

    @app.post('/api/conversations/<uuid:cid>/messages')
    @api.require_auth
    def append_message(cid):
        body, error = validate_body()
        if error:
            return error
        with database(api) as cur:
            previous = existing_request(cur, body['request_id'], cid)
            if previous is not None:
                return previous
            cur.execute('SELECT * FROM conversations WHERE id = %s FOR UPDATE', (str(cid),))
            conversation = cur.fetchone()
            if not conversation:
                return jsonify(error='not_found'), 404
            cur.execute("SELECT 1 FROM conversation_jobs WHERE conversation_id = %s AND status IN ('queued', 'running')",
                        (str(cid),))
            if cur.fetchone():
                return jsonify(error='conversation_busy'), 409
            cur.execute('SELECT role, position FROM conversation_messages WHERE conversation_id = %s ORDER BY position DESC LIMIT 1',
                        (str(cid),))
            last = cur.fetchone()
            if last and last['role'] != 'assistant':
                return jsonify(error='unanswered_message', detail='Retry or delete the unanswered message first.'), 409
            cur.execute('SELECT count(*) AS count, COALESCE(sum(length(content)), 0) AS chars FROM conversation_messages WHERE conversation_id = %s', (str(cid),))
            size = cur.fetchone()
            if size['count'] >= MAX_CONVERSATION_MESSAGES - 1 or size['chars'] + len(body['prompt']) > MAX_CONVERSATION_CHARS:
                return jsonify(error='conversation_too_long'), 400
            if not api.MODELS[body['model']].get('supports_images', True):
                cur.execute('SELECT 1 FROM conversation_message_images mi JOIN conversation_messages m ON m.id = mi.message_id WHERE m.conversation_id = %s LIMIT 1', (str(cid),))
                if cur.fetchone():
                    return jsonify(error='images_not_supported'), 400
            return enqueue(cur, str(cid), body, conversation['revision'], last['position'] + 1 if last else 0)

    @app.post('/api/conversations/<uuid:cid>/retry')
    @api.require_auth
    def retry_message(cid):
        with database(api) as cur:
            cur.execute('SELECT revision FROM conversations WHERE id = %s FOR UPDATE', (str(cid),))
            if not cur.fetchone():
                return jsonify(error='not_found'), 404
            cur.execute('SELECT * FROM conversation_jobs WHERE conversation_id = %s ORDER BY created_at DESC LIMIT 1 FOR UPDATE', (str(cid),))
            job = cur.fetchone()
            if not job or job['status'] != 'failed':
                return jsonify(error='not_retryable'), 409
            cur.execute("UPDATE conversation_jobs SET status = 'queued', started_at = NULL, finished_at = NULL, error_code = NULL, error_detail = NULL WHERE id = %s", (str(job['id']),))
            return jsonify(job_id=str(job['id'])), 202

    def mutation_request(cur, cid, mid, body, kind):
        request_id = body.get('request_id')
        try:
            request_id = str(UUID(request_id))
        except (ValueError, TypeError, AttributeError):
            return None, (jsonify(error='invalid_request_id'), 400)
        cur.execute('SELECT pg_advisory_xact_lock(478, hashtext(%s))', (request_id,))
        cur.execute('SELECT * FROM conversation_mutations WHERE request_id = %s', (request_id,))
        previous = cur.fetchone()
        if previous:
            if str(previous['conversation_id']) != str(cid) or str(previous['message_id']) != str(mid) or previous['kind'] != kind:
                return None, (jsonify(error='request_id_conflict'), 409)
            return None, (jsonify(previous['response']), previous['status'])
        cur.execute('SELECT 1 FROM conversation_jobs WHERE id = %s', (request_id,))
        if cur.fetchone():
            return None, (jsonify(error='request_id_conflict'), 409)
        return request_id, None

    def remember_mutation(cur, cid, mid, request_id, kind, response, status=200):
        cur.execute('INSERT INTO conversation_mutations (request_id, conversation_id, message_id, kind, response, status) VALUES (%s, %s, %s, %s, %s, %s)',
                    (request_id, str(cid), str(mid), kind, psycopg2.extras.Json(response), status))
        return jsonify(response), status

    def invalidate_tail(cur, cid, position, edited_mid=None):
        cur.execute('SELECT mi.image_sha FROM conversation_message_images mi JOIN conversation_messages m ON m.id = mi.message_id WHERE m.conversation_id = %s AND m.position >= %s', (str(cid), position))
        shas = {r['image_sha'] for r in cur.fetchall()}
        if edited_mid:
            cur.execute('UPDATE conversation_share_snapshots SET revoked = TRUE WHERE message_id IN (SELECT snapshot_id FROM conversation_share_sources WHERE message_id = %s)', (str(edited_mid),))
            cur.execute('DELETE FROM conversation_share_images WHERE snapshot_id IN (SELECT snapshot_id FROM conversation_share_sources WHERE message_id = %s)', (str(edited_mid),))
            cur.execute('DELETE FROM conversation_share_sources WHERE snapshot_id IN (SELECT snapshot_id FROM conversation_share_sources WHERE message_id = %s)', (str(edited_mid),))
            cur.execute('UPDATE conversation_messages SET share_token = NULL WHERE id = %s', (str(edited_mid),))
        cur.execute('DELETE FROM conversation_jobs WHERE user_message_id IN (SELECT id FROM conversation_messages WHERE conversation_id = %s AND position >= %s)', (str(cid), position))
        cur.execute('DELETE FROM conversation_messages WHERE conversation_id = %s AND position > %s', (str(cid), position))
        cur.execute("""UPDATE conversations SET revision = revision + 1, updated_at = now(),
                    title_status = CASE WHEN %s <= 1 AND title_status IN ('queued', 'running')
                        THEN 'failed' ELSE title_status END WHERE id = %s""", (position, str(cid)))
        return shas

    def validate_model_images(cur, cid, model, position, images):
        if not isinstance(model, str) or model not in api.SELECTABLE_MODELS:
            return jsonify(error='unknown_model'), 400
        if not api.MODELS[model].get('supports_images', True):
            cur.execute('SELECT 1 FROM conversation_message_images mi JOIN conversation_messages m ON m.id = mi.message_id WHERE m.conversation_id = %s AND m.position < %s LIMIT 1', (str(cid), position))
            if images or cur.fetchone():
                return jsonify(error='images_not_supported'), 400
        return None

    @app.patch('/api/conversations/<uuid:cid>/messages/<uuid:mid>')
    @api.require_auth
    def edit_message(cid, mid):
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get('content'), str):
            return jsonify(error='invalid_request'), 400
        content = body['content']
        if len(content) > MAX_MESSAGE_CHARS:
            return jsonify(error='invalid_content'), 400
        if type(body.get('revision')) is not int:
            return jsonify(error='invalid_revision'), 400
        with database(api) as cur:
            request_id, previous = mutation_request(cur, cid, mid, body, 'edit')
            if previous is not None:
                return previous
            cur.execute('SELECT revision FROM conversations WHERE id = %s FOR UPDATE', (str(cid),))
            conversation = cur.fetchone()
            if not conversation:
                return jsonify(error='not_found'), 404
            if conversation['revision'] != body['revision']:
                return jsonify(error='conversation_changed', detail='The conversation changed. Reload it before saving.'), 409
            cur.execute('SELECT * FROM conversation_messages WHERE id = %s AND conversation_id = %s', (str(mid), str(cid)))
            message = cur.fetchone()
            if not message:
                return jsonify(error='not_found'), 404
            position = message['position']
            cur.execute('SELECT mi.image_sha, i.byte_size FROM conversation_message_images mi JOIN images i ON i.sha256 = mi.image_sha WHERE mi.message_id = %s ORDER BY mi.position', (str(mid),))
            original_images = cur.fetchall()
            original_shas = [i['image_sha'] for i in original_images]
            shas, uploads = list(original_shas), []
            if 'images' in body:
                if message['role'] != 'user' or not isinstance(body['images'], list):
                    return jsonify(error='invalid_images'), 400
                requested = body['images']
                if len(requested) > api.MAX_IMAGES:
                    return jsonify(error='too_many_images'), 400
                retained = {i['image_sha']: i['byte_size'] for i in original_images}
                new_images = []
                for image in requested:
                    if not isinstance(image, dict):
                        return jsonify(error='invalid_images'), 400
                    if 'sha' in image:
                        if set(image) != {'sha'} or not isinstance(image['sha'], str) or image['sha'] not in retained:
                            return jsonify(error='invalid_image_reference'), 400
                    else:
                        new_images.append(image)
                error, uploads = api.validate_images(new_images)
                if error:
                    return jsonify(error=error[0], detail=error[1]), 400
                decoded = iter(uploads)
                shas = [image['sha'] if 'sha' in image else next(decoded)['sha256'] for image in requested]
                sizes = {**retained, **{i['sha256']: len(i['raw']) for i in uploads}}
                if sum(sizes[sha] for sha in shas) > api.MAX_TOTAL_IMAGE_MB * 1_000_000:
                    return jsonify(error='images_too_large_total'), 400
            if not content.strip() and (message['role'] != 'user' or not shas):
                return jsonify(error='invalid_content'), 400
            cur.execute("SELECT model FROM conversation_messages WHERE conversation_id = %s AND position = %s AND role = 'assistant'", (str(cid), position + 1))
            answer = cur.fetchone()
            original_model = answer['model'] if answer else message['model']
            model = body.get('model', original_model)
            changed = content != message['content'] or shas != original_shas or (message['role'] == 'user' and model != original_model)
            if not changed:
                return remember_mutation(cur, cid, mid, request_id, 'edit', dict(ok=True, changed=False))
            if message['role'] == 'user':
                error = validate_model_images(cur, cid, model, position, shas)
                if error:
                    return error
            cur.execute('SELECT COALESCE(sum(length(content)), 0) AS chars FROM conversation_messages WHERE conversation_id = %s AND position < %s', (str(cid), position))
            if cur.fetchone()['chars'] + len(content) > MAX_CONVERSATION_CHARS:
                return jsonify(error='conversation_too_long'), 400
            orphan_shas = invalidate_tail(cur, cid, position, mid)
            if 'images' in body:
                api.store_images(cur, mid, uploads, conversation=True, attach=False)
                cur.execute('DELETE FROM conversation_message_images WHERE message_id = %s', (str(mid),))
                for index, sha in enumerate(shas):
                    cur.execute('INSERT INTO conversation_message_images (message_id, image_sha, position) VALUES (%s, %s, %s)', (str(mid), sha, index))
            cur.execute('UPDATE conversation_messages SET content = %s, edited_at = now() WHERE id = %s', (content, str(mid)))
            api.delete_orphan_images(cur, orphan_shas)
            response = dict(ok=True, changed=True)
            status = 200
            if message['role'] == 'user':
                cur.execute('UPDATE conversation_messages SET model = %s WHERE id = %s', (model, str(mid)))
                cur.execute('INSERT INTO conversation_jobs (id, conversation_id, user_message_id, model, revision) VALUES (%s, %s, %s, %s, %s)', (request_id, str(cid), str(mid), model, conversation['revision'] + 1))
                response.update(id=str(cid), job_id=request_id, message_id=str(mid), model=model)
                status = 202
            return remember_mutation(cur, cid, mid, request_id, 'edit', response, status)

    @app.post('/api/conversations/<uuid:cid>/messages/<uuid:mid>/regenerate')
    @api.require_auth
    def regenerate_answer(cid, mid):
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or type(body.get('revision')) is not int:
            return jsonify(error='invalid_revision'), 400
        with database(api) as cur:
            request_id, previous = mutation_request(cur, cid, mid, body, 'regenerate')
            if previous is not None:
                return previous
            cur.execute('SELECT revision FROM conversations WHERE id = %s FOR UPDATE', (str(cid),))
            conversation = cur.fetchone()
            if not conversation:
                return jsonify(error='not_found'), 404
            if conversation['revision'] != body['revision']:
                return jsonify(error='conversation_changed', detail='The conversation changed. Reload it before regenerating.'), 409
            cur.execute("SELECT position, model FROM conversation_messages WHERE id = %s AND conversation_id = %s AND role = 'assistant'", (str(mid), str(cid)))
            answer = cur.fetchone()
            if not answer:
                return jsonify(error='not_found'), 404
            cur.execute("SELECT id, position FROM conversation_messages WHERE conversation_id = %s AND position = %s AND role = 'user'", (str(cid), answer['position'] - 1))
            question = cur.fetchone()
            if not question:
                return jsonify(error='missing_question'), 409
            cur.execute('SELECT image_sha FROM conversation_message_images WHERE message_id = %s', (str(question['id']),))
            images = [row['image_sha'] for row in cur.fetchall()]
            model = body.get('model', answer['model'])
            error = validate_model_images(cur, cid, model, question['position'], images)
            if error:
                return error
            shas = invalidate_tail(cur, cid, question['position'])
            cur.execute('UPDATE conversation_messages SET model = %s WHERE id = %s', (model, str(question['id'])))
            cur.execute('INSERT INTO conversation_jobs (id, conversation_id, user_message_id, model, revision) VALUES (%s, %s, %s, %s, %s)', (request_id, str(cid), str(question['id']), model, conversation['revision'] + 1))
            api.delete_orphan_images(cur, shas)
            return remember_mutation(cur, cid, mid, request_id, 'regenerate',
                                     dict(id=str(cid), job_id=request_id, message_id=str(question['id']), model=model), 202)

    @app.delete('/api/conversations/<uuid:cid>/messages/<uuid:mid>')
    @api.require_auth
    def truncate_conversation(cid, mid):
        with database(api) as cur:
            cur.execute('SELECT id FROM conversations WHERE id = %s FOR UPDATE', (str(cid),))
            if not cur.fetchone():
                return jsonify(error='not_found'), 404
            cur.execute("SELECT position FROM conversation_messages WHERE id = %s AND conversation_id = %s AND role = 'user'", (str(mid), str(cid)))
            row = cur.fetchone()
            if not row:
                return jsonify(error='not_found'), 404
            cur.execute('SELECT mi.image_sha FROM conversation_message_images mi JOIN conversation_messages m ON m.id = mi.message_id WHERE m.conversation_id = %s AND m.position >= %s', (str(cid), row['position']))
            shas = {r['image_sha'] for r in cur.fetchall()}
            if row['position'] == 0:
                cur.execute('DELETE FROM conversations WHERE id = %s', (str(cid),))
            else:
                cur.execute('UPDATE conversations SET revision = revision + 1, updated_at = now() WHERE id = %s', (str(cid),))
                cur.execute('DELETE FROM conversation_messages WHERE conversation_id = %s AND position >= %s', (str(cid), row['position']))
            api.delete_orphan_images(cur, shas)
            return jsonify(ok=True, deleted_conversation=row['position'] == 0)

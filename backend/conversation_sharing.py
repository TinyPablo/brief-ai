"""Public links expose one answer and its immediate question, never chat context."""

import secrets

from flask import jsonify, request
from psycopg2.extras import Json
from conversations import database, image_metadata, json_row


def public_answer(api, token):
    with database(api) as cur:
        cur.execute('SELECT payload, revoked FROM conversation_share_snapshots WHERE token = %s', (token,))
        snapshot = cur.fetchone()
        if snapshot:
            return None if snapshot['revoked'] else snapshot['payload']
        cur.execute(
            """SELECT a.*, u.content AS prompt, u.id AS question_id
               FROM conversation_messages a JOIN conversation_messages u
               ON u.conversation_id = a.conversation_id AND u.position = a.position - 1
               WHERE a.share_token = %s AND a.role = 'assistant'""", (token,),
        )
        row = cur.fetchone()
        if not row:
            return None
        images = [{'id': str(row['question_id'])}]
        if row['share_show_images']:
            image_metadata(cur, images)
        return dict(created_at=row['created_at'].isoformat(),
                    model_label=api.MODELS.get(row['model'], {}).get('label', row['model']),
                    prompt=row['prompt'] if row['share_show_prompt'] else None,
                    answer=row['content'], charts=row.get('charts', []), images=images[0].get('images', []))


def image_allowed(api, token, sha):
    with database(api) as cur:
        cur.execute('SELECT message_id, revoked FROM conversation_share_snapshots WHERE token = %s', (token,))
        snapshot = cur.fetchone()
        if snapshot:
            if snapshot['revoked']:
                return False
            cur.execute('SELECT 1 FROM conversation_share_images WHERE snapshot_id = %s AND image_sha = %s',
                        (str(snapshot['message_id']), sha))
            return cur.fetchone() is not None
        cur.execute(
            """SELECT 1 FROM conversation_messages a JOIN conversation_messages u
               ON u.conversation_id = a.conversation_id AND u.position = a.position - 1
               JOIN conversation_message_images mi ON mi.message_id = u.id
               WHERE a.share_token = %s AND a.share_show_images AND a.role = 'assistant'
               AND mi.image_sha = %s""", (token, sha),
        )
        return cur.fetchone() is not None


def register_sharing(app, api):
    @app.get('/api/conversation-messages/<uuid:mid>')
    @api.require_auth
    def message_sharing_settings(mid):
        with database(api) as cur:
            cur.execute("""SELECT CASE WHEN s.revoked THEN NULL ELSE m.share_token END AS share_token,
                           m.share_show_prompt, m.share_show_images,
                           CASE WHEN s.revoked THEN NULL ELSE s.scope END AS share_scope, s.created_at AS snapshot_created_at
                           FROM conversation_messages m LEFT JOIN conversation_share_snapshots s ON s.message_id = m.id
                           WHERE m.id = %s AND m.role = 'assistant'""", (str(mid),))
            row = cur.fetchone()
            if not row:
                return jsonify(error='not_found'), 404
            return jsonify(**json_row(row), prompt_is_raw=True)

    @app.route('/api/conversation-messages/<uuid:mid>/share', methods=['POST', 'PATCH', 'DELETE'])
    @api.require_auth
    def share_message(mid):
        options = request.get_json(silent=True) if request.data else {}
        if not isinstance(options, dict) or any(key not in {'scope', 'show_prompt', 'show_images'} for key in options):
            return jsonify(error='invalid_share_options'), 400
        for key in ('show_prompt', 'show_images'):
            if key in options and type(options[key]) is not bool:
                return jsonify(error='invalid_share_options'), 400
        scope = options.get('scope', 'conversation')
        if scope not in ('answer', 'exchange', 'conversation'):
            return jsonify(error='invalid_share_scope'), 400
        # Older clients keep their question visibility option; included messages
        # always retain their attachments in new snapshots.
        if 'scope' not in options and 'show_prompt' in options:
            scope = 'exchange' if options['show_prompt'] else 'answer'
        with database(api) as cur:
            cur.execute("SELECT conversation_id FROM conversation_messages WHERE id = %s AND role = 'assistant'", (str(mid),))
            found = cur.fetchone()
            if not found:
                return jsonify(error='not_found'), 404
            cur.execute('SELECT title FROM conversations WHERE id = %s FOR UPDATE', (str(found['conversation_id']),))
            conversation = cur.fetchone()
            cur.execute("SELECT * FROM conversation_messages WHERE id = %s AND role = 'assistant' FOR UPDATE", (str(mid),))
            anchor = cur.fetchone()
            if not anchor or not conversation:
                return jsonify(error='not_found'), 404
            if request.method == 'DELETE':
                cur.execute('DELETE FROM conversation_share_snapshots WHERE message_id = %s', (str(mid),))
                cur.execute('UPDATE conversation_messages SET share_token = NULL WHERE id = %s', (str(mid),))
                return jsonify(ok=True)
            if request.method == 'PATCH' and not anchor['share_token']:
                return jsonify(error='not_found'), 404
            if request.method == 'PATCH' and 'scope' not in options and 'show_prompt' not in options:
                cur.execute('SELECT scope FROM conversation_share_snapshots WHERE message_id = %s', (str(mid),))
                current_scope = cur.fetchone()
                scope = current_scope['scope'] if current_scope else ('exchange' if anchor['share_show_prompt'] else 'answer')
            token = anchor['share_token'] or 'c_' + secrets.token_urlsafe(16)
            start = anchor['position'] if scope == 'answer' else anchor['position'] - 1 if scope == 'exchange' else 0
            cur.execute('SELECT * FROM conversation_messages WHERE conversation_id = %s AND position BETWEEN %s AND %s ORDER BY position',
                        (str(found['conversation_id']), start, anchor['position']))
            source = [dict(row) for row in cur.fetchall()]
            image_metadata(cur, source)
            messages = [dict(role=row['role'], content=row['content'], created_at=row['created_at'].isoformat(),
                             charts=row.get('charts', []),
                             model_label=api.MODELS.get(row['model'], {}).get('label', row['model']), images=row['images'])
                        for row in source]
            payload = dict(scope=scope, title=conversation['title'], messages=messages,
                           created_at=anchor['created_at'].isoformat(),
                           answer=anchor['content'], charts=anchor.get('charts', []), model_label=api.MODELS.get(anchor['model'], {}).get('label', anchor['model']),
                           prompt=source[-2]['content'] if scope != 'answer' else None,
                           images=source[-2]['images'] if scope != 'answer' else [])
            cur.execute('DELETE FROM conversation_share_snapshots WHERE message_id = %s', (str(mid),))
            cur.execute("INSERT INTO conversation_share_snapshots(message_id, token, scope, payload) VALUES (%s, %s, %s, %s) RETURNING created_at",
                        (str(mid), token, scope, Json(payload)))
            created = cur.fetchone()['created_at'].isoformat()
            for row in source:
                cur.execute('INSERT INTO conversation_share_sources(snapshot_id, message_id) VALUES (%s, %s)', (str(mid), str(row['id'])))
                for image in row['images']:
                    cur.execute('INSERT INTO conversation_share_images(snapshot_id, image_sha) VALUES (%s, %s) ON CONFLICT DO NOTHING', (str(mid), image['sha']))
            cur.execute('UPDATE conversation_messages SET share_token = %s, share_show_prompt = %s, share_show_images = TRUE WHERE id = %s',
                        (token, scope != 'answer', str(mid)))
            return jsonify(token=token, scope=scope, snapshot_created_at=created,
                           message_count=len(messages), show_prompt=scope != 'answer', show_images=True)

    @app.get('/api/conversation-shares')
    @api.require_auth
    def list_conversation_shares():
        before = request.args.get('before')
        try:
            from uuid import UUID
            before = str(UUID(before)) if before else None
        except ValueError:
            return jsonify(error='invalid_cursor'), 400
        with database(api) as cur:
            cur.execute(
                """SELECT a.id, a.conversation_id, a.created_at, a.model, a.share_token,
                          LEFT(u.content, 140) AS prompt_preview
                   FROM conversation_messages a JOIN conversation_messages u
                   ON u.conversation_id = a.conversation_id AND u.position = a.position - 1
                   LEFT JOIN conversation_share_snapshots s ON s.message_id = a.id
                   WHERE a.share_token IS NOT NULL AND COALESCE(s.revoked, FALSE) = FALSE AND (%s IS NULL OR (a.created_at, a.id) <
                       (SELECT created_at, id FROM conversation_messages WHERE id = %s))
                   ORDER BY a.created_at DESC, a.id DESC LIMIT 30""", (before, before),
            )
            return jsonify(items=[json_row(row) for row in cur.fetchall()])

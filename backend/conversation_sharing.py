"""Public links expose one answer and its immediate question, never chat context."""

import secrets

from flask import jsonify, request
from conversations import database, image_metadata, json_row


def public_answer(api, token):
    with database(api) as cur:
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
                    answer=row['content'], images=images[0].get('images', []))


def image_allowed(api, token, sha):
    with database(api) as cur:
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
            cur.execute("SELECT share_token, share_show_prompt, share_show_images FROM conversation_messages WHERE id = %s AND role = 'assistant'", (str(mid),))
            row = cur.fetchone()
            return jsonify(**dict(row), prompt_is_raw=True) if row else (jsonify(error='not_found'), 404)

    @app.route('/api/conversation-messages/<uuid:mid>/share', methods=['POST', 'PATCH', 'DELETE'])
    @api.require_auth
    def share_message(mid):
        options = request.get_json(silent=True) if request.data else {}
        if not isinstance(options, dict) or any(key not in {'show_prompt', 'show_images'} or type(value) is not bool for key, value in options.items()):
            return jsonify(error='Share options must be boolean values.'), 400
        with database(api) as cur:
            cur.execute("SELECT share_token, share_show_prompt, share_show_images FROM conversation_messages WHERE id = %s AND role = 'assistant' FOR UPDATE", (str(mid),))
            row = cur.fetchone()
            if not row:
                return jsonify(error='not_found'), 404
            if request.method == 'DELETE':
                cur.execute('UPDATE conversation_messages SET share_token = NULL WHERE id = %s', (str(mid),))
                return jsonify(ok=True)
            if request.method == 'PATCH' and not row['share_token']:
                return jsonify(error='not_found'), 404
            token = row['share_token'] or 'c_' + secrets.token_urlsafe(16)
            show_prompt = options.get('show_prompt', row['share_show_prompt'])
            show_images = options.get('show_images', row['share_show_images'])
            cur.execute('UPDATE conversation_messages SET share_token = %s, share_show_prompt = %s, share_show_images = %s WHERE id = %s', (token, show_prompt, show_images, str(mid)))
            return jsonify(token=token, show_prompt=show_prompt, show_images=show_images)

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
                   WHERE a.share_token IS NOT NULL AND (%s IS NULL OR (a.created_at, a.id) <
                       (SELECT created_at, id FROM conversation_messages WHERE id = %s))
                   ORDER BY a.created_at DESC, a.id DESC LIMIT 30""", (before, before),
            )
            return jsonify(items=[json_row(row) for row in cur.fetchall()])

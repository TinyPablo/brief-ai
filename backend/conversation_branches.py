"""Independent conversation copies share attachment bytes, not message rows."""

from uuid import UUID, uuid4

from flask import jsonify, request
from conversations import database


def register_branches(app, api):
    @app.post('/api/conversations/<uuid:cid>/branches')
    @api.require_auth
    def branch_conversation(cid):
        body = request.get_json(silent=True)
        try:
            mid = str(UUID(body['message_id']))
            request_id = str(UUID(body['request_id']))
        except (KeyError, ValueError, TypeError, AttributeError):
            return jsonify(error='invalid_branch_request'), 400
        with database(api) as cur:
            cur.execute('SELECT pg_advisory_xact_lock(480, hashtext(%s))', (request_id,))
            cur.execute('SELECT id, branch_source_conversation_id, branch_source_message_id FROM conversations WHERE branch_request_id = %s', (request_id,))
            existing = cur.fetchone()
            if existing:
                if str(existing['branch_source_conversation_id']) != str(cid) or str(existing['branch_source_message_id']) != mid:
                    return jsonify(error='request_id_conflict'), 409
                return jsonify(id=str(existing['id'])), 201
            cur.execute('SELECT * FROM conversations WHERE id = %s FOR UPDATE', (str(cid),))
            source = cur.fetchone()
            if not source:
                return jsonify(error='not_found'), 404
            cur.execute("SELECT position FROM conversation_messages WHERE id = %s AND conversation_id = %s AND role = 'assistant'", (mid, str(cid)))
            anchor = cur.fetchone()
            if not anchor:
                return jsonify(error='not_found'), 404
            destination = str(uuid4())
            title = source['title'][:90] + ' · branch' if source['title'] else None
            cur.execute(
                '''INSERT INTO conversations(id, context, title, title_status, branch_request_id, branch_source_conversation_id, branch_source_message_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)''',
                (destination, source['context'], title, 'completed' if title else 'waiting', request_id, str(cid), mid),
            )
            cur.execute('SELECT * FROM conversation_messages WHERE conversation_id = %s AND position <= %s ORDER BY position', (str(cid), anchor['position']))
            for message in cur.fetchall():
                new_id = str(uuid4())
                cur.execute(
                    '''INSERT INTO conversation_messages
                       (id, conversation_id, position, role, content, created_at, model, brief,
                        input_tokens, output_tokens, cost_usd, duration_ms, stop_reason, reasoning, inherited)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)''',
                    (new_id, destination, message['position'], message['role'], message['content'],
                     message['created_at'], message['model'], message['brief'], message['input_tokens'],
                     message['output_tokens'], message['cost_usd'], message['duration_ms'], message['stop_reason'], message['reasoning']),
                )
                cur.execute(
                    '''INSERT INTO conversation_message_images(message_id, image_sha, position)
                       SELECT %s, image_sha, position FROM conversation_message_images WHERE message_id = %s''',
                    (new_id, str(message['id'])),
                )
            return jsonify(id=destination), 201

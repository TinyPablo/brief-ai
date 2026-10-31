"""Postgres notifications are wakeups; durable conversation rows remain authoritative."""

import select
import json
import time
from uuid import UUID

from flask import Response

WORK_CHANNEL = 'brief_conversation_work'
CHANGE_CHANNEL = 'brief_conversation_changes'


class Listener:
    def __init__(self, api, channel):
        if channel not in {WORK_CHANNEL, CHANGE_CHANNEL}:
            raise ValueError('Unknown notification channel')
        self.conn = api.get_db()
        try:
            self.conn.autocommit = True
            with self.conn.cursor() as cur:
                cur.execute('LISTEN ' + channel)
        except Exception:
            self.conn.close()
            raise

    def wait(self, timeout=5):
        self.conn.poll()
        if not self.conn.notifies:
            if not select.select([self.conn], [], [], timeout)[0]:
                return []
            self.conn.poll()
        notifications = self.conn.notifies[:]
        self.conn.notifies.clear()
        return [notice.payload for notice in notifications]

    def close(self):
        self.conn.close()


def register_events(app, api):
    @app.get('/api/conversation-events')
    @api.require_auth
    def conversation_events():
        def stream():
            listener = None
            try:
                listener = Listener(api, CHANGE_CHANNEL)
                # The client refreshes snapshots after LISTEN has taken effect.
                yield 'retry: 1000\nevent: ready\ndata: {}\n\n'
                end = time.monotonic() + 55
                while time.monotonic() < end:
                    changes = listener.wait(timeout=min(10, max(0, end - time.monotonic())))
                    for cid in set(changes):
                        try:
                            cid = str(UUID(cid))
                        except (ValueError, TypeError):
                            continue
                        yield 'event: changed\ndata: ' + json.dumps({'id': cid}) + '\n\n'
                    if not changes:
                        yield ': keepalive\n\n'
            except Exception:
                app.logger.exception('Conversation event connection interrupted')
                yield 'event: unavailable\ndata: {}\n\n'
            finally:
                if listener is not None:
                    listener.close()
        return Response(stream(), mimetype='text/event-stream', headers={
            'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no',
        })

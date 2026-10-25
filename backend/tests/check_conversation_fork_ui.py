"""Browser flow for branching, share scopes and public thread rendering."""

import copy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from uuid import uuid4

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


class QuietHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/s/'):
            self.path = '/index.html'
        super().do_GET()

    def log_message(self, *args):
        pass


def check_fork_ui():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'frontend/public')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cid, fork_id = str(uuid4()), str(uuid4())
    stamp = '2026-10-24T12:00:00Z'
    def message(role, text):
        return dict(id=str(uuid4()), role=role, content=text, model='gemini', model_label='Gemini', created_at=stamp,
                    images=[], input_tokens=10, output_tokens=5, duration_ms=100, cost_usd=0.001)
    turns = [message('user', 'Question A'), message('assistant', 'Answer B'), message('user', 'Question C'), message('assistant', 'Answer D')]
    original = dict(id=cid, title='Original chat', context='', revision=0, messages=turns, job={'status': 'completed'})
    chats = {cid: original}
    share_bodies, branches, errors, edits, generations = [], [], [], [], []
    token = 'c_' + 'x' * 22
    payload = None
    def route_api(route):
        nonlocal payload
        path = route.request.url.split('/api', 1)[1]
        method = route.request.method
        body = route.request.post_data_json if route.request.post_data else {}
        data, status = {}, 200
        if path == '/session':
            data = {'authenticated': True}
        elif path == '/config':
            data = {'models': [dict(id='gemini', label='Gemini', provider='google', input=1, output=2,
                                   context_window=1000000, supports_images=True)], 'default': 'gemini', 'usd_pln': 4}
        elif path.startswith('/conversations?'):
            data = {'items': [dict(id=cid, title='Original chat', prompt_preview='Question A', message_count=4, created_at=stamp, status='completed')]}
        elif path.startswith('/history?'):
            data = {'items': []}
        elif path == '/conversations/' + cid + '/branches':
            branches.append(body)
            copied = copy.deepcopy(original)
            copied['id'], copied['title'] = fork_id, 'Original chat · branch'
            index = next(i for i, m in enumerate(turns) if m['id'] == body['message_id'])
            copied['messages'] = copied['messages'][:index + 1]
            for m in copied['messages']:
                m['id'] = str(uuid4())
            chats[fork_id] = copied
            data, status = {'id': fork_id}, 201
        elif method == 'PATCH' and '/messages/' in path:
            edits.append(body)
            chat = chats[path.split('/')[2]]
            index = next(i for i, m in enumerate(chat['messages']) if m['id'] == path.rsplit('/', 1)[1])
            chat['messages'] = chat['messages'][:index + 1]
            chat['messages'][-1].update(content=body['content'], edited_at=stamp)
            chat['revision'] += 1
            chat['job'] = None
        elif path.endswith('/generate'):
            generations.append(body)
            chat = chats[path.split('/')[2]]
            chat['messages'].append(message('assistant', 'Answer to edited question'))
            chat['job'] = {'status': 'completed'}
            data, status = {'id': chat['id']}, 202
        elif path.startswith('/conversations/'):
            data = chats[path.rsplit('/', 1)[1]]
        elif path.startswith('/conversation-messages/') and path.endswith('/share'):
            share_bodies.append(body)
            anchor = path.split('/')[2]
            current = next(chat for chat in chats.values() if any(m['id'] == anchor for m in chat['messages']))
            index = next(i for i, m in enumerate(current['messages']) if m['id'] == anchor)
            scope = body['scope']
            selected = current['messages'][index:index + 1] if scope == 'answer' else current['messages'][index - 1:index + 1] if scope == 'exchange' else current['messages'][:index + 1]
            payload = dict(scope=scope, messages=selected, created_at=stamp, answer=selected[-1]['content'], prompt=None, model_label='Gemini', images=[])
            data = {'token': token, 'scope': scope, 'snapshot_created_at': stamp}
        elif path.startswith('/conversation-messages/'):
            data = {'share_token': None, 'share_scope': None, 'prompt_is_raw': True}
        elif path == '/share/' + token:
            data = payload
        else:
            data, status = {'error': 'not_found'}, 404
        route.fulfill(status=status, content_type='application/json', body=json.dumps(data))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
            context = browser.new_context(viewport={'width': 1280, 'height': 1000})
            context.route('**/api/**', route_api)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.get_by_role('button', name='history', exact=True).click()
            page.get_by_role('button', name='Original chat').click()
            active = page.locator('[data-conversation-view]:not([hidden])')
            expect(active.get_by_text('Answer D', exact=True)).to_be_visible()
            active.get_by_role('button', name='Branch', exact=True).first.click()
            expect(active).to_have_attribute('data-conversation-id', fork_id)
            expect(active.get_by_text('Answer B', exact=True)).to_be_visible()
            expect(active.get_by_text('Question C', exact=True)).to_have_count(0)
            assert branches[0]['message_id'] == turns[1]['id']
            assert branches[0]['request_id']
            page.get_by_role('button', name='Previous conversation: Original chat', exact=True).click()
            expect(active.get_by_text('Answer D', exact=True)).to_be_visible()
            active.get_by_role('button', name='Share', exact=True).last.click()
            expect(page.get_by_role('radio', name='Conversation up to this answer', exact=True)).to_be_checked()
            expect(page.get_by_role('checkbox')).to_have_count(0)
            page.get_by_role('button', name='Publish link', exact=True).click()
            expect(page.get_by_label('Public link')).to_be_visible()
            assert share_bodies[-1] == {'scope': 'conversation'}
            public = context.new_page()
            public.goto(f'http://127.0.0.1:{server.server_port}/s/' + token)
            for text in ['Question A', 'Answer B', 'Question C', 'Answer D']:
                expect(public.get_by_text(text, exact=True)).to_be_visible()
            expect(public.get_by_text('Branch', exact=True)).to_have_count(0)
            page.get_by_role('radio', name='Only this answer', exact=True).check()
            page.get_by_role('button', name='Update snapshot', exact=True).click()
            assert share_bodies[-1] == {'scope': 'answer'}
            public.reload()
            expect(public.get_by_text('Answer D', exact=True)).to_be_visible()
            expect(public.get_by_text('Question A', exact=True)).to_have_count(0)
            page.get_by_role('radio', name='Question and answer', exact=True).check()
            page.get_by_role('button', name='Update snapshot', exact=True).click()
            assert share_bodies[-1] == {'scope': 'exchange'}
            public.reload()
            expect(public.get_by_text('Question C', exact=True)).to_be_visible()
            expect(public.get_by_text('Answer D', exact=True)).to_be_visible()
            expect(public.get_by_text('Question A', exact=True)).to_have_count(0)
            page.get_by_role('button', name='Close dialog', exact=True).click()
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile message toolbar overflow'
            page.screenshot(path='/private/tmp/brief-forks-mobile.png', full_page=True)
            # Cancel leaves the thread intact; saving never generates a response.
            active.get_by_role('button', name='Edit', exact=True).last.click()
            active.get_by_role('textbox', name='Edit message', exact=True).fill('Discard this edit')
            active.get_by_role('button', name='Cancel', exact=True).click()
            expect(active.get_by_text('Answer D', exact=True)).to_be_visible()
            assert not edits
            active.get_by_role('button', name='Edit', exact=True).nth(1).click()
            active.get_by_role('textbox', name='Edit message', exact=True).fill('Corrected answer B')
            active.get_by_role('button', name='Save', exact=True).click()
            expect(active.get_by_text('Corrected answer B', exact=True)).to_be_visible()
            expect(active.get_by_text('Question C', exact=True)).to_have_count(0)
            expect(active.get_by_role('button', name='Generate answer', exact=True)).to_have_count(0)
            expect(active.get_by_role('button', name='Edit', exact=True)).to_have_count(2)
            assert len(edits) == 1 and not generations
            active.get_by_placeholder('Follow up…').fill('Unsent follow-up draft')
            expect(active.get_by_role('button', name='send', exact=True)).to_be_enabled()
            active.get_by_role('button', name='Edit', exact=True).first.click()
            active.get_by_role('textbox', name='Edit message', exact=True).fill('Corrected question A')
            active.get_by_role('button', name='Save', exact=True).click()
            expect(active.get_by_text('Corrected answer B', exact=True)).to_have_count(0)
            expect(active.get_by_role('button', name='Generate answer', exact=True)).to_be_visible()
            assert len(edits) == 2 and not generations
            expect(active.get_by_placeholder('Follow up…')).to_have_value('Unsent follow-up draft')
            expect(active.get_by_role('button', name='send', exact=True)).to_be_disabled()
            active.get_by_role('button', name='Generate answer', exact=True).click()
            expect(active.get_by_text('Answer to edited question', exact=True)).to_be_visible()
            assert len(generations) == 1 and generations[0]['request_id']
            page.reload()
            expect(page.get_by_text('Corrected question A', exact=True)).to_be_visible()
            expect(page.get_by_text('Answer to edited question', exact=True)).to_be_visible()
            assert not errors, errors
            browser.close()
            print('PASS: branch action/placement, source retained, default full snapshot, all scope controls, public thread, mobile toolbar, edit/cancel/truncation, explicit generation and reload')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    check_fork_ui()

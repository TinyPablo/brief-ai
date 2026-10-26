"""Browser checks with a local static server and fake API; no model calls.

Requires Playwright and Chrome. Run directly from the repository root.
"""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from uuid import uuid4

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def check_ui():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'frontend/public')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cid = str(uuid4())
    stamp = '2026-10-24T12:32:08+00:00'
    messages = []
    state = {'id': cid, 'context': 'private context', 'messages': messages, 'job': None}
    requests = []
    shares = []
    complete = False
    errors = []
    first_request = None

    def message(role, text, model='gemini-3.1-flash-lite'):
        return dict(id=str(uuid4()), role=role, content=text, model=model, created_at=stamp,
                    duration_ms=1200, input_tokens=10, output_tokens=5, cost_usd=0.001, images=[])

    def route_api(route):
        nonlocal complete, first_request
        path = route.request.url.split('/api', 1)[1]
        method = route.request.method
        body = route.request.post_data_json if method == 'POST' and route.request.post_data else {}
        status, data = 200, {}
        if path == '/session':
            data = {'authenticated': True}
        elif path == '/config':
            data = {'models': [dict(id=m, label=label, provider=provider, provider_label=provider,
                                   input=1, output=2, context_window=1000000, supports_images=True)
                               for m, label, provider in [('gemini-3.1-flash-lite', 'Gemini', 'google'), ('claude-haiku-4-5', 'Claude', 'anthropic')]],
                    'default': 'gemini-3.1-flash-lite', 'usd_pln': 4}
        elif path == '/conversations' and method == 'POST':
            first_request = body['request_id']
            requests.append(body)
            messages.append(message('user', body['prompt'], body['model']))
            state['job'] = {'status': 'queued'}
            data, status = {'id': cid}, 202
        elif path == '/conversations/' + cid + '/messages' and method == 'POST':
            requests.append(body)
            messages.append(message('user', body['prompt'], body['model']))
            state['job'] = {'status': 'queued'}
            complete = False
            data, status = {'id': cid}, 202
        elif path == '/conversation-requests/' + str(first_request):
            data = {'id': cid}
        elif path == '/conversations/' + cid and method == 'GET':
            if complete and messages[-1]['role'] == 'user':
                messages.append(message('assistant', 'Answer ' + str(len(requests)), requests[-1]['model']))
                state['job'] = {'status': 'completed'}
            data = state
        elif path == '/conversations/' + cid + '/retry':
            state['job'] = {'status': 'queued'}
            data, status = {'job_id': str(uuid4())}, 202
        elif path.startswith('/conversations/' + cid + '/messages/') and method == 'DELETE':
            mid = path.rsplit('/', 1)[1]
            index = next(i for i, m in enumerate(messages) if m['id'] == mid)
            del messages[index:]
            state['job'] = {'status': 'completed'}
            data = {'ok': True, 'deleted_conversation': index == 0}
        elif path.startswith('/conversations?'):
            data = {'items': [dict(id=cid, prompt_preview='First question', message_count=len(messages), created_at=stamp, status='completed')]}
        elif path.startswith('/history?'):
            data = {'items': []}
        elif path.startswith('/conversation-messages/') and path.endswith('/share'):
            shares.append(body)
            data = {'token': 'c_' + 'x' * 22, 'show_prompt': body.get('show_prompt', True), 'show_images': body.get('show_images', False)}
        elif path.startswith('/conversation-messages/'):
            data = {'prompt_is_raw': True, 'share_token': None, 'share_show_prompt': True, 'share_show_images': False}
        else:
            status, data = 404, {'error': 'not_found'}
        route.fulfill(status=status, content_type='application/json', body=json.dumps(data))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/**', route_api)
            page.goto(f'http://127.0.0.1:{server.server_port}')
            active = page.locator('[data-conversation-view]:not([hidden])')
            textarea = active.get_by_placeholder('Ask anything…')
            textarea.fill('First question')
            with page.expect_response(lambda response: response.url.endswith('/api/conversations') and response.request.method == 'POST'):
                page.get_by_role('button', name='brief', exact=True).click()
            expect(active.get_by_text('First question', exact=True)).to_be_visible()
            assert requests[0]['request_id']
            page.reload()
            expect(active.get_by_text('First question', exact=True)).to_be_visible()
            complete = True
            expect(active.get_by_text('Answer 1', exact=True)).to_be_visible(timeout=10000)
            active.locator('select').select_option('claude-haiku-4-5')
            active.get_by_placeholder('Follow up…').fill('Follow-up question')
            with page.expect_response(lambda response: response.url.endswith('/messages') and response.request.method == 'POST'):
                page.get_by_role('button', name='send', exact=True).click()
            expect(active.get_by_text('Follow-up question', exact=True)).to_be_visible()
            assert requests[1]['model'] == 'claude-haiku-4-5'
            assert requests[0]['request_id'] != requests[1]['request_id']
            complete = True
            expect(active.get_by_text('Answer 2', exact=True)).to_be_visible(timeout=10000)
            page.get_by_role('button', name='Share', exact=True).last.click()
            expect(page.get_by_role('dialog')).to_be_visible()
            # Sharing the current answer keeps the existing explicit publish action.
            page.get_by_role('button', name='Publish link', exact=True).click()
            expect(page.get_by_label('Public link')).to_be_visible()
            assert shares and shares[0]['scope'] == 'conversation'
            page.get_by_role('button', name='Close dialog').click()
            page.get_by_role('button', name='Delete message and later replies').last.click()
            expect(page.get_by_role('dialog')).to_have_count(0)
            page.get_by_role('button', name='Confirm deletion of message and later replies').click()
            expect(active.get_by_text('Follow-up question', exact=True)).to_have_count(0)
            expect(active.get_by_text('Answer 1', exact=True)).to_be_visible()
            # The history opens a saved conversation; old entries keep their existing view.
            page.get_by_role('button', name='history', exact=True).click()
            page.get_by_role('button', name='First question').click()
            expect(active.get_by_text('Answer 1', exact=True)).to_be_visible()
            page.get_by_role('button', name='New conversation', exact=True).click()
            expect(active.get_by_placeholder('Ask anything…')).to_be_visible()
            expect(active.get_by_text('Answer 1', exact=True)).to_have_count(0)
            # Recover a submission acknowledged by the server before the browser stored its ID.
            page = browser.new_page(viewport={'width': 1280, 'height': 1000})
            page.add_init_script("sessionStorage.setItem('briefai_pending_request', " + json.dumps(first_request) + ");")
            page.route('**/api/**', route_api)
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            active = page.locator('[data-conversation-view]:not([hidden])')
            expect(active.get_by_text('Answer 1', exact=True)).to_be_visible()
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path='/private/tmp/brief-conversation-mobile.png', full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile horizontal overflow'
            assert not errors, errors
            browser.close()
            print('PASS: browser send, refresh during generation, follow-up model switch, sharing, truncation, history, new conversation, request recovery and mobile layout')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    check_ui()

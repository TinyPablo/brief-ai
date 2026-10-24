"""Browser scenarios for multiple conversations, drafts and background replies."""

import base64
from collections import Counter
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from uuid import uuid4

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5S8AAAAASUVORK5CYII=')


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def check_sessions():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'frontend/public')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    conversations, submissions = {}, {}
    reads = Counter()
    errors = []
    stamp = '2026-10-24T12:32:08+00:00'
    done = set()
    last_created = None
    held_routes = []

    def message(role, content, model):
        return dict(id=str(uuid4()), role=role, content=content, model=model, created_at=stamp,
                    duration_ms=100, input_tokens=10, output_tokens=5, cost_usd=0.001, images=[])

    def route_api(route):
        nonlocal last_created
        path = route.request.url.split('/api', 1)[1]
        method = route.request.method
        body = route.request.post_data_json if route.request.post_data else {}
        status, data = 200, {}
        if path == '/session':
            data = {'authenticated': True}
        elif path == '/config':
            data = {'models': [dict(id=model, label=label, provider=provider, input=1, output=2,
                                    context_window=1000000, supports_images=True)
                               for model, label, provider in [('gemini', 'Gemini', 'google'), ('claude', 'Claude', 'anthropic')]],
                    'default': 'gemini', 'usd_pln': 4}
        elif path == '/conversations' and method == 'POST':
            cid = str(uuid4())
            last_created = cid
            conversations[cid] = dict(id=cid, messages=[message('user', body['prompt'], body['model'])],
                                      job={'status': 'queued'}, context='')
            submissions[body['request_id']] = cid
            data, status = {'id': cid}, 202
            if body['prompt'] == 'Race question':
                held_routes.append((route, cid))
                return
        elif path.startswith('/conversations/') and path.endswith('/messages') and method == 'POST':
            cid = path.split('/')[2]
            conversations[cid]['messages'].append(message('user', body['prompt'], body['model']))
            conversations[cid]['job'] = {'status': 'queued'}
            submissions[body['request_id']] = cid
            done.discard(cid)
            data, status = {'id': cid}, 202
        elif path.startswith('/conversation-requests/'):
            cid = submissions.get(path.rsplit('/', 1)[1])
            data, status = ({'id': cid}, 200) if cid else ({'error': 'not_found'}, 404)
        elif path.startswith('/conversations/') and method == 'GET':
            cid = path.rsplit('/', 1)[1]
            reads[cid] += 1
            if cid not in conversations:
                data, status = {'error': 'not_found'}, 404
            else:
                data = conversations[cid]
                if cid in done and data['messages'][-1]['role'] == 'user':
                    data['messages'].append(message('assistant', 'Reply to ' + data['messages'][-1]['content'], data['messages'][0]['model']))
                    data['job'] = {'status': 'completed'}
        elif path.startswith('/conversations?'):
            data = {'items': [dict(id=cid, prompt_preview=c['messages'][0]['content'], message_count=len(c['messages']), created_at=stamp, status=c['job']['status']) for cid, c in conversations.items()]}
        elif path.startswith('/history?'):
            data = {'items': []}
        else:
            status, data = 404, {'error': 'not_found'}
        route.fulfill(status=status, content_type='application/json', body=json.dumps(data))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
            context = browser.new_context(viewport={'width': 1280, 'height': 900})
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/**', route_api)
            page.goto(f'http://127.0.0.1:{server.server_port}')
            active = page.locator('[data-conversation-view]:not([hidden])')
            views = page.locator('[data-conversation-view]')
            new = page.get_by_role('button', name='New conversation', exact=True)
            textarea = active.locator('textarea')
            expect(textarea).to_be_visible()
            new.click()
            assert views.count() == 1
            textarea.fill('Draft A')
            active.locator('select').select_option('claude')
            active.locator('input[type=file]').set_input_files({'name': 'photo.png', 'mimeType': 'image/png', 'buffer': PNG})
            expect(active.get_by_role('button', name='Remove image 1')).to_be_visible()
            new.click()
            expect(views).to_have_count(2)
            textarea.fill('Draft B')
            page.get_by_role('button', name='Previous conversation: Draft A').click()
            expect(textarea).to_have_value('Draft A')
            expect(active.locator('select')).to_have_value('claude')
            expect(active.get_by_role('button', name='Remove image 1')).to_be_visible()
            page.reload()
            expect(textarea).to_have_value('Draft A')
            expect(active.locator('select')).to_have_value('claude')
            expect(active.get_by_role('button', name='Remove image 1')).to_be_visible()
            page.get_by_role('button', name='Next conversation: Draft B').click()
            expect(textarea).to_have_value('Draft B')
            expect(active.locator('select')).to_have_value('gemini')
            # Closing a draft requires confirmation; cancel keeps it unchanged.
            page.get_by_role('button', name='Close conversation', exact=True).click()
            expect(page.get_by_role('dialog', name='Discard draft?')).to_be_visible()
            page.get_by_role('button', name='Keep open', exact=True).click()
            expect(textarea).to_have_value('Draft B')
            page.get_by_role('button', name='Close conversation', exact=True).click()
            page.get_by_role('button', name='Discard and close', exact=True).click()
            expect(views).to_have_count(1)
            expect(textarea).to_have_value('Draft A')
            # Start A, then B without waiting for A's model response.
            textarea.fill('Question A')
            with page.expect_response(lambda r: r.url.endswith('/api/conversations') and r.request.method == 'POST'):
                active.get_by_role('button', name='brief', exact=True).click()
            cid_a = last_created
            expect(active).to_have_attribute('data-conversation-id', cid_a)
            new.click()
            textarea.fill('Question B')
            with page.expect_response(lambda r: r.url.endswith('/api/conversations') and r.request.method == 'POST'):
                active.get_by_role('button', name='brief', exact=True).click()
            cid_b = last_created
            expect(active).to_have_attribute('data-conversation-id', cid_b)
            done.add(cid_a)
            expect(page.get_by_role('button', name='Previous conversation: Question A')).to_contain_text('New reply', timeout=10000)
            done.add(cid_b)
            expect(active.get_by_text('Reply to Question B', exact=True)).to_be_visible(timeout=10000)
            read_counts = dict(reads)
            page.get_by_role('button', name='Previous conversation: Question A').click()
            expect(active.get_by_text('Reply to Question A', exact=True)).to_be_visible()
            page.get_by_role('button', name='Next conversation: Question B').click()
            expect(active.get_by_text('Reply to Question B', exact=True)).to_be_visible()
            assert dict(reads) == read_counts, 'Switching fetched already cached conversations'
            # History reuses an existing view, instead of duplicating or refetching it.
            page.get_by_role('button', name='history', exact=True).click()
            page.get_by_role('button', name='Question A').click()
            expect(active).to_have_attribute('data-conversation-id', cid_a)
            expect(views).to_have_count(2)
            assert dict(reads) == read_counts
            # A new draft is inserted immediately to the right, and empty drafts are reused.
            new.click()
            expect(views).to_have_count(3)
            assert views.nth(1).get_attribute('data-conversation-id') == ''
            empty_key = active.get_attribute('data-conversation-view')
            new.click()
            expect(views).to_have_count(3)
            assert active.get_attribute('data-conversation-view') == empty_key
            page.get_by_role('button', name='Close conversation', exact=True).click()
            expect(active).to_have_attribute('data-conversation-id', cid_b)
            expect(views).to_have_count(2)
            # Reading earlier messages stays in place when a background answer arrives.
            long_text = '\n\n'.join('Paragraph ' + str(i) + ': A longer answer for scrolling.' for i in range(60))
            conversations[cid_b]['messages'][1]['content'] = long_text
            page.reload()
            expect(active.get_by_text('Paragraph 59:', exact=False)).to_be_visible(timeout=10000)
            page.evaluate('window.scrollTo(0, 400)')
            page.wait_for_function("JSON.parse(sessionStorage.getItem('briefai_workspace_v1')).active !== null")
            # Wait for the throttled scroll snapshot to reach IndexedDB.
            page.wait_for_timeout(180)
            page.get_by_role('button', name='Previous conversation: Question A').click()
            page.get_by_role('button', name='Next conversation: Question B').click()
            page.wait_for_function('Math.abs(scrollY - 400) < 5')
            page.reload()
            page.wait_for_function('Math.abs(scrollY - 400) < 5')
            textarea.fill('Continue B')
            with page.expect_response(lambda r: r.url.endswith('/messages') and r.request.method == 'POST'):
                active.get_by_role('button', name='send', exact=True).click()
            expect(active.locator('.whitespace-pre-wrap').filter(has_text='Continue B')).to_be_visible()
            page.evaluate('window.scrollTo(0, 400)')
            page.wait_for_timeout(180)
            done.add(cid_b)
            expect(page.get_by_role('button', name='New reply ↓')).to_be_visible(timeout=10000)
            assert abs(page.evaluate('scrollY') - 400) < 5, 'New reply interrupted reading position'
            page.get_by_role('button', name='New reply ↓').click()
            page.wait_for_function('document.documentElement.scrollHeight - innerHeight - scrollY < 80')
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path='/private/tmp/brief-sessions-mobile.png')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile overflow'
            page.get_by_role('button', name='Close conversation', exact=True).click()
            expect(views).to_have_count(1)
            page.get_by_role('button', name='Close conversation', exact=True).click()
            expect(views).to_have_count(1)
            expect(active).to_have_attribute('data-conversation-id', '')
            expect(textarea).to_have_value('')
            # A saved conversation opened before its create acknowledgement must merge
            # with the original pending view, preserving a single cached instance.
            textarea.fill('Race question')
            with page.expect_request(lambda r: r.url.endswith('/api/conversations') and r.method == 'POST'):
                active.get_by_role('button', name='brief', exact=True).click()
            page.set_viewport_size({'width': 1280, 'height': 900})
            page.get_by_role('button', name='history', exact=True).click()
            page.get_by_role('button', name='Race question').click()
            expect(views).to_have_count(2)
            held, race_id = held_routes.pop()
            held.fulfill(status=202, content_type='application/json', body=json.dumps({'id': race_id}))
            expect(views).to_have_count(1)
            expect(active).to_have_attribute('data-conversation-id', race_id)
            done.add(race_id)
            expect(active.get_by_text('Reply to Race question', exact=True)).to_be_visible(timeout=10000)
            # A conversation removed elsewhere must not silently discard its local draft.
            textarea.fill('Keep after external deletion')
            page.wait_for_timeout(180)
            conversations.pop(race_id)
            page.reload()
            expect(active).to_have_attribute('data-conversation-id', '')
            expect(textarea).to_have_value('Keep after external deletion')
            blocked = browser.new_context()
            blocked.add_init_script("Object.defineProperty(window, 'indexedDB', { value: { open() { throw new Error('blocked'); } } });")
            fallback = blocked.new_page()
            fallback.route('**/api/**', route_api)
            fallback.goto(f'http://127.0.0.1:{server.server_port}')
            expect(fallback.get_by_role('alert')).to_contain_text('Draft changes could not be saved locally')
            fallback.get_by_placeholder('Ask anything…').fill('Memory-only draft')
            fallback.get_by_role('button', name='New conversation', exact=True).click()
            fallback.get_by_role('button', name='Previous conversation: Memory-only draft').click()
            expect(fallback.locator('[data-conversation-view]:not([hidden]) textarea')).to_have_value('Memory-only draft')
            blocked.close()
            assert not errors, errors
            browser.close()
            print('PASS: multiple drafts/images, per-view models, refresh, close confirmation, background jobs, cached switching, history deduplication, insertion order, empty reuse, scroll restoration and mobile layout')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    check_sessions()

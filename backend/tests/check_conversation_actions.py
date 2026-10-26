"""Browser checks for inline attachments, header actions, regeneration and recovery."""

import base64
import copy
from functools import partial
import hashlib
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


def check_actions():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'frontend/public')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stamp = '2026-10-26T12:00:00Z'
    def message(role, content, model='gemini', images=None):
        return dict(id=str(uuid4()), role=role, content=content, model=model, images=images or [], created_at=stamp,
                    input_tokens=10, output_tokens=5, cost_usd=0.001, duration_ms=100)
    cid = str(uuid4())
    original = dict(id=cid, title='Original chat', revision=0, messages=[message('user', 'Question A'), message('assistant', 'Answer B'), message('user', 'Question C'), message('assistant', 'Answer D')], job={'status': 'completed'})
    chats = {cid: original}
    receipts, edits, regenerations, submissions, retries, errors = {}, [], [], [], [], []
    abort_once = {'edit': False, 'send': False}
    def images_from_body(images):
        return [dict(sha=image['sha'], byte_size=68, media_type='image/png', has_thumb=False, width=1, height=1) if 'sha' in image else dict(sha=hashlib.sha256(base64.b64decode(image['data'])).hexdigest(), byte_size=len(base64.b64decode(image['data'])), media_type=image['media_type'], width=1, height=1, has_thumb=False) for image in images]
    def complete(chat, model, text='New answer'):
        chat['messages'].append(message('assistant', text, model))
        chat['job'] = {'status': 'completed'}
    def route_api(route):
        path = route.request.url.split('/api', 1)[1]
        method = route.request.method
        body = route.request.post_data_json if route.request.post_data else {}
        status, data = 200, {}
        if path == '/session':
            data = {'authenticated': True}
        elif path == '/config':
            data = {'models': [dict(id=m, label=label, provider=provider, context_window=1000000, input=1, output=2, supports_images=True) for m, label, provider in [('gemini', 'Gemini', 'google'), ('claude', 'Claude', 'anthropic')]], 'default': 'gemini', 'usd_pln': 4}
        elif path.startswith('/images/'):
            route.fulfill(content_type='image/png', body=PNG)
            return
        elif path.startswith('/conversations?'):
            data = {'items': [dict(id=chat['id'], title=chat['title'], prompt_preview=chat['messages'][0]['content'], created_at=stamp, message_count=len(chat['messages']), status=chat['job']['status']) for chat in chats.values()]}
        elif path.startswith('/history?'):
            data = {'items': []}
        elif path.startswith('/conversation-requests/'):
            receipt = receipts.get(path.rsplit('/', 1)[1])
            if receipt:
                data = receipt
            else:
                data, status = {'error': 'not_found'}, 404
        elif method == 'PATCH' and '/messages/' in path:
            request_id = body['request_id']
            if request_id in receipts:
                data = receipts[request_id]
            else:
                chat = chats[path.split('/')[2]]
                index = next(i for i, m in enumerate(chat['messages']) if m['id'] == path.rsplit('/', 1)[1])
                edited = chat['messages'][index]
                response_model = chat['messages'][index + 1]['model'] if index + 1 < len(chat['messages']) and edited['role'] == 'user' else edited['model']
                files = images_from_body(body.get('images', [{'sha': im['sha']} for im in edited['images']]))
                changed = body['content'] != edited['content'] or [i['sha'] for i in files] != [i['sha'] for i in edited['images']] or (edited['role'] == 'user' and body['model'] != response_model)
                data = {'ok': True, 'changed': changed}
                if changed:
                    edits.append(copy.deepcopy(body))
                    chat['messages'] = chat['messages'][:index + 1]
                    edited.update(content=body['content'], images=files, edited_at=stamp)
                    chat['revision'] += 1
                    if edited['role'] == 'user':
                        complete(chat, body['model'], 'Answer to edited message')
                        data.update(id=chat['id'], model=body['model'])
                receipts[request_id] = data
                if abort_once['edit']:
                    abort_once['edit'] = False
                    route.abort('failed')
                    return
        elif path.endswith('/regenerate'):
            request_id = body['request_id']
            if request_id in receipts:
                data = receipts[request_id]
            else:
                regenerations.append(body)
                chat = chats[path.split('/')[2]]
                index = next(i for i, m in enumerate(chat['messages']) if m['id'] == path.split('/')[-2])
                chat['messages'] = chat['messages'][:index]
                chat['revision'] += 1
                complete(chat, body['model'], 'Regenerated answer')
                data = {'id': chat['id'], 'model': body['model']}
                receipts[request_id] = data
        elif path.endswith('/retry'):
            retries.append(path)
            chat = chats[path.split('/')[2]]
            complete(chat, chat['messages'][-1]['model'], 'Recovered answer')
            data, status = {'job_id': str(uuid4())}, 202
        elif method == 'POST' and (path == '/conversations' or path.endswith('/messages')):
            request_id = body['request_id']
            if request_id in receipts:
                data = receipts[request_id]
            else:
                submissions.append(copy.deepcopy(body))
                if path == '/conversations':
                    chat = dict(id=str(uuid4()), title='Image chat', messages=[], revision=0, job={'status': 'queued'})
                    chats[chat['id']] = chat
                else:
                    chat = chats[path.split('/')[2]]
                chat['messages'].append(message('user', body['prompt'], body['model'], images_from_body(body['images'])))
                complete(chat, body['model'], 'Answer to image')
                data = {'id': chat['id']}
                receipts[request_id] = data
                if abort_once['send']:
                    abort_once['send'] = False
                    route.fulfill(status=502, content_type='application/json', body=json.dumps({'error': 'Temporary gateway error'}))
                    return
            status = 202
        elif path.startswith('/conversations/'):
            data = chats[path.split('/')[2]]
        else:
            data, status = {'error': 'not_found'}, 404
        route.fulfill(status=status, content_type='application/json', body=json.dumps(data))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
            context = browser.new_context(viewport={'width': 1360, 'height': 1000})
            context.route('**/api/**', route_api)
            page = context.new_page()
            page.set_default_timeout(8000)
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.get_by_role('button', name='history', exact=True).click()
            page.get_by_role('button', name='Original chat').click()
            active = page.locator('[data-conversation-view]:not([hidden])')
            draft = active.get_by_placeholder('Follow up…')
            draft.fill('Keep this draft')
            active.get_by_role('button', name='Edit', exact=True).first.click()
            expect(active.get_by_role('textbox', name='Edit message')).to_have_value('Question A')
            assert active.get_by_role('textbox', name='Edit message').evaluate('(element) => element.selectionStart === element.value.length && element.selectionEnd === element.value.length')
            style = '(element) => { const s = getComputedStyle(element); return [s.appearance, s.fontFamily, s.fontSize, s.padding, s.borderRadius, s.backgroundColor]; }'
            assert active.get_by_role('combobox', name='Edit model').evaluate(style) == active.get_by_role('combobox', name='Model', exact=True).evaluate(style)

            expect(active.get_by_text('Answer D', exact=True)).to_be_visible()
            expect(active.get_by_role('button', name='send', exact=True)).to_be_disabled()
            active.get_by_role('button', name='Save', exact=True).click()
            expect(active.get_by_role('textbox', name='Edit message')).to_have_count(0)
            expect(active.get_by_text('Answer D', exact=True)).to_be_visible()
            assert not edits
            active.get_by_role('button', name='Edit', exact=True).first.click()
            active.get_by_role('textbox', name='Edit message').fill('Discard when leaving')
            page.get_by_role('button', name='history', exact=True).click()
            page.get_by_role('button', name='Original chat').click()
            expect(active.get_by_role('textbox', name='Edit message')).to_have_count(0)
            expect(draft).to_have_value('Keep this draft')
            # A choice does not mutate the conversation or remount the panel.
            active.get_by_role('button', name='Regenerate', exact=True).first.click()
            dialog = page.get_by_role('dialog', name='Regenerate answer')
            dialog.evaluate('(element) => { element.dataset.identity = "original"; }')
            before = dialog.bounding_box()
            dialog.get_by_role('combobox', name='Reply model').select_option('claude')
            expect(dialog).to_have_attribute('data-identity', 'original')
            after = dialog.bounding_box()
            assert dialog.get_by_role('combobox', name='Reply model').evaluate(style) == active.get_by_role('combobox', name='Model', exact=True).evaluate(style)

            assert abs(after['height'] - before['height']) < 1
            assert dialog.evaluate('(element) => element.getAnimations().length') == 0
            assert not regenerations
            expect(active.locator('select').last).to_have_value('gemini')
            page.keyboard.press('Escape')
            expect(dialog).to_have_count(0)
            expect(active.get_by_text('Answer D', exact=True)).to_be_visible()
            active.get_by_role('button', name='Regenerate', exact=True).first.click()
            expect(dialog).to_be_visible()
            page.evaluate('window.scrollTo(0, window.scrollY > 50 ? 0 : 200)')
            expect(dialog).to_have_count(0)
            assert not regenerations
            active.get_by_role('button', name='Regenerate', exact=True).first.click()
            dialog.get_by_role('combobox', name='Reply model').select_option('claude')
            dialog.get_by_role('button', name='Regenerate', exact=True).click()
            expect(active.get_by_text('Regenerated answer', exact=True)).to_be_visible()
            expect(active.get_by_text('Question C', exact=True)).to_have_count(0)
            expect(active.locator('select').last).to_have_value('claude')
            expect(draft).to_have_value('Keep this draft')
            assert len(regenerations) == 1
            # Attachments and reply model are edited locally until Save.
            active.get_by_role('button', name='Edit', exact=True).first.click()
            editor = active.locator('.message-editor')
            picker = editor.get_by_role('button', name='Attach images to message')
            picker.hover()
            expect(picker.locator('.attachment-details')).to_be_visible()
            with page.expect_file_chooser() as chooser:
                picker.click()
            expect(picker.locator('.attachment-details')).to_be_hidden()
            chooser.value.set_files({'name': 'photo.png', 'mimeType': 'image/png', 'buffer': PNG})
            expect(editor.get_by_role('button', name='Remove image 1')).to_be_visible()
            expect(picker.locator('.attachment-details')).to_be_hidden()
            page.mouse.move(0, 0)
            picker.hover()
            expect(picker.locator('.attachment-details')).to_be_visible()
            editor.get_by_role('textbox', name='Edit message').evaluate('''(element, data) => {
                const clipboard = new DataTransfer();
                clipboard.items.add(new File([Uint8Array.from(atob(data), c => c.charCodeAt(0))], 'pasted.png', {type: 'image/png'}));
                element.dispatchEvent(new ClipboardEvent('paste', {clipboardData: clipboard, bubbles: true}));
            }''', base64.b64encode(PNG).decode())
            expect(editor.get_by_role('button', name='Remove image 2')).to_be_visible()
            expect(picker.locator('.attachment-details')).to_be_hidden()
            editor.get_by_role('button', name='Remove image 2').click()
            editor.get_by_role('textbox', name='Edit message').fill('')
            editor.get_by_role('combobox', name='Edit model').select_option('gemini')
            abort_once['edit'] = True
            editor.get_by_role('button', name='Save', exact=True).click()
            expect(editor.get_by_role('button', name='Try again', exact=True)).to_be_visible()
            editor.get_by_role('button', name='Try again', exact=True).click()
            expect(active.get_by_text('Answer to edited message', exact=True)).to_be_visible()
            assert len(edits) == 1 and edits[-1]['content'] == '' and len(edits[-1]['images']) == 1
            expect(active.locator('select').last).to_have_value('gemini')
            expect(draft).to_have_value('Keep this draft')
            active.get_by_role('button', name='Edit', exact=True).first.click()
            editor.get_by_role('button', name='Remove image 1').click()
            expect(editor.get_by_role('button', name='Save', exact=True)).to_be_disabled()
            editor.get_by_role('button', name='Cancel editing').click()
            assert len(original['messages'][0]['images']) == 1
            # Failed generation retries the stored question, rather than submitting it twice.
            original['messages'].pop()
            original['job'] = {'status': 'failed', 'error_detail': 'Temporary model error'}
            page.reload()
            expect(active.get_by_role('button', name='Try again', exact=True)).to_be_visible()
            active.get_by_role('button', name='Try again', exact=True).click()
            expect(active.get_by_text('Recovered answer', exact=True)).to_be_visible()
            assert len(retries) == 1 and len(original['messages']) == 2
            page.set_viewport_size({'width': 390, 'height': 844})
            active.get_by_role('button', name='Regenerate', exact=True).first.click()
            expect(dialog).to_be_visible()
            box = dialog.bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= 390.5, box
            assert box['y'] >= 0 and box['y'] + box['height'] <= 844.5, box
            dialog.get_by_role('combobox', name='Reply model').select_option('claude')
            page.screenshot(path='/private/tmp/brief-actions-mobile.png', full_page=False)
            page.keyboard.press('Escape')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile overflow'
            # A lost initial acknowledgement can be retried with the same captured request.
            page.get_by_role('button', name='New conversation', exact=True).click()
            active.locator('input[type=file]').set_input_files({'name': 'photo.png', 'mimeType': 'image/png', 'buffer': PNG})
            expect(active.get_by_role('button', name='brief', exact=True)).to_be_enabled()
            abort_once['send'] = True
            active.get_by_role('button', name='brief', exact=True).click()
            expect(active.get_by_role('button', name='Try again', exact=True)).to_be_visible()
            active.get_by_role('button', name='Try again', exact=True).click()
            expect(active.get_by_text('Answer to image', exact=True)).to_be_visible()
            assert len(submissions) == 1 and submissions[0]['prompt'] == ''
            active.locator('input[type=file]').set_input_files({'name': 'photo.png', 'mimeType': 'image/png', 'buffer': PNG})
            expect(active.get_by_role('button', name='send', exact=True)).to_be_enabled()
            active.get_by_role('button', name='send', exact=True).click()
            expect(active.get_by_text('Answer to image', exact=True)).to_have_count(2)
            assert len(submissions) == 2 and submissions[-1]['prompt'] == ''
            assert not errors, errors
            browser.close()
            print('PASS: inline edit/no-op/cancel, attachments, independent draft, model selection, stable regeneration popover, recovery, image-only turns and mobile bounds')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    check_actions()

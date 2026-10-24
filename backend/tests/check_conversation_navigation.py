"""Browser checks for gestures, transitions, asynchronous titles and Settings."""

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


def check_navigation():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'frontend/public')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cid = str(uuid4())
    body = {}
    reads = 0
    errors = []
    stamp = '2026-10-24T12:00:00Z'

    def route_api(route):
        nonlocal body, reads
        path = route.request.url.split('/api', 1)[1]
        status, data = 200, {}
        if path == '/session':
            data = {'authenticated': True}
        elif path == '/config':
            data = {'models': [dict(id='gemini', label='Gemini', provider='google', input=1,
                                   output=2, context_window=1000000, supports_images=True)],
                    'default': 'gemini', 'usd_pln': 4}
        elif path == '/conversations' and route.request.method == 'POST':
            body = route.request.post_data_json
            data, status = {'id': cid}, 202
        elif path == '/conversations/' + cid:
            reads += 1
            answer = '\n\n'.join('Paragraph ' + str(i) + ': Content for scrolling.' for i in range(40)) + '\n\n```python\nprint("' + 'long_code_' * 40 + '")\n```'
            messages = [dict(id='user', role='user', content='Alpha', model='gemini', created_at=stamp, images=[]),
                        dict(id='answer', role='assistant', content=answer, model='gemini', created_at=stamp,
                             images=[], input_tokens=10, output_tokens=20, cost_usd=0.001, duration_ms=100)]
            data = dict(id=cid, context='', messages=messages, job={'status': 'completed'},
                        title_status='completed' if reads > 1 else 'queued', title='Weekend w Krakowie' if reads > 1 else None)
        else:
            data, status = {'error': 'not_found'}, 404
        route.fulfill(status=status, content_type='application/json', body=json.dumps(data))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, has_touch=True, reduced_motion='no-preference')
            context.add_init_script('''window.motion = []; const original = Element.prototype.animate;
              Element.prototype.animate = function(frames, options) { window.motion.push({frames,options}); return original.call(this,frames,options); };''')
            page = context.new_page()
            page.on('pageerror', lambda error: (errors.append(str(error)), print('Browser error:', error, flush=True)))
            page.route('**/api/**', route_api)
            page.goto(f'http://127.0.0.1:{server.server_port}')
            active = page.locator('[data-conversation-view]:not([hidden])')
            expect(active.locator('textarea')).to_be_visible()
            expect(page.get_by_role('button', name='Close conversation', exact=True)).to_be_visible()
            expect(page.locator('.conversation-actions')).to_have_count(0)
            page.evaluate('window.motion = []')
            page.get_by_role('button', name='New conversation', exact=True).click()
            page.wait_for_function("window.motion.some(a=>a.frames.some(f=>String(f.transform).includes('scale')))")
            expect(page.get_by_text('Edit settings →', exact=True)).to_have_count(0)
            expect(page.get_by_role('checkbox')).to_have_count(0)
            page.get_by_role('button', name='settings', exact=True).click()
            expect(page.get_by_role('checkbox')).to_have_count(3)
            page.get_by_label('Location context', exact=True).fill('Kraków')
            page.get_by_role('checkbox').nth(2).check()
            page.get_by_label('Personal context', exact=True).fill('Prefer Polish')
            page.get_by_role('button', name='← Back to prompt', exact=True).click()
            active.locator('textarea').fill('Alpha')
            with page.expect_response(lambda r: r.url.endswith('/api/conversations') and r.request.method == 'POST'):
                active.get_by_role('button', name='brief', exact=True).click()
            expect(active).to_have_attribute('data-conversation-id', cid)
            assert 'Kraków' in body['context'] and 'Prefer Polish' in body['context']
            # A completed answer keeps polling only while its title job is pending.
            page.get_by_role('button', name='New conversation', exact=True).click()
            expect(page.get_by_role('button', name='Previous conversation: Weekend w Krakowie')).to_be_visible(timeout=10000)
            assert reads >= 2
            active.locator('textarea').fill('Beta')
            side = page.get_by_role('button', name='Previous conversation: Weekend w Krakowie').bounding_box()
            stage = page.locator('.conversation-stage').bounding_box()
            assert side['x'] + side['width'] <= stage['x'], 'Side label overlaps conversation'
            page.screenshot(path='/private/tmp/brief-navigation-desktop.png', full_page=True)
            page.evaluate('window.motion = []')
            page.get_by_role('button', name='New conversation', exact=True).click()
            active.locator('textarea').fill('Gamma')
            page.wait_for_function('window.motion.length >= 2', timeout=3000)
            # Trackpad momentum is consumed as one gesture, rather than skipping chats.
            page.evaluate("document.querySelector('[data-conversation-view]:not([hidden])').dispatchEvent(new WheelEvent('wheel', {deltaX:-90,bubbles:true,cancelable:true}))")
            expect(active.locator('textarea')).to_have_value('Beta')
            for _ in range(5):
                page.evaluate("document.querySelector('[data-conversation-view]:not([hidden])').dispatchEvent(new WheelEvent('wheel', {deltaX:-100,bubbles:true,cancelable:true}))")
            expect(active.locator('textarea')).to_have_value('Beta')
            page.wait_for_timeout(240)
            page.evaluate("document.querySelector('[data-conversation-view]:not([hidden])').dispatchEvent(new WheelEvent('wheel', {deltaX:-90,bubbles:true,cancelable:true}))")
            expect(active).to_have_attribute('data-conversation-id', cid)
            page.wait_for_timeout(350)
            position = active.bounding_box()
            page.mouse.move(position['x'] + 20, 300)
            before = page.evaluate('scrollY')
            page.mouse.wheel(0, -500)
            page.mouse.wheel(0, 700)
            page.wait_for_function('scrollY > 0')
            assert active.get_attribute('data-conversation-id') == cid
            assert not active.locator('textarea').evaluate("el => { const e=new WheelEvent('wheel',{deltaX:200,bubbles:true,cancelable:true});el.dispatchEvent(e);return e.defaultPrevented; }")
            assert not active.locator('pre').evaluate("el => { const e=new WheelEvent('wheel',{deltaX:200,bubbles:true,cancelable:true});el.dispatchEvent(e);return e.defaultPrevented; }")
            assert not active.evaluate("el => { const e=new WheelEvent('wheel',{deltaX:200,ctrlKey:true,bubbles:true,cancelable:true});el.dispatchEvent(e);return e.defaultPrevented; }")
            # Swipe in the message area; vertical swipes must keep the active chat.
            page.evaluate('''() => {
              const el=document.querySelector('[data-conversation-view]:not([hidden])');
              const point=(x,y)=>new Touch({identifier:1,target:el,clientX:x,clientY:y});
              el.dispatchEvent(new TouchEvent('touchstart',{touches:[point(250,300)],bubbles:true}));
              el.dispatchEvent(new TouchEvent('touchmove',{touches:[point(100,305)],bubbles:true,cancelable:true}));
              el.dispatchEvent(new TouchEvent('touchend',{touches:[],changedTouches:[point(100,305)],bubbles:true}));
            }''')
            expect(active.locator('textarea')).to_have_value('Beta')
            page.get_by_role('button', name='Close conversation', exact=True).click()
            expect(page.get_by_role('dialog', name='Discard draft?')).to_be_visible()
            page.evaluate("window.dispatchEvent(new WheelEvent('wheel',{deltaX:300,bubbles:true,cancelable:true}))")
            expect(active.locator('textarea')).to_have_value('Beta')
            page.get_by_role('button', name='Keep open', exact=True).click()
            page.emulate_media(reduced_motion='reduce')
            page.evaluate('window.motion = []')
            page.get_by_role('button', name='Next conversation: Gamma').click()
            expect(active.locator('textarea')).to_have_value('Gamma')
            assert not page.evaluate('window.motion'), 'Reduced motion still animates'
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path='/private/tmp/brief-navigation-mobile.png', full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile horizontal overflow'
            assert not errors, errors
            browser.close()
            print('PASS: Settings-only context, async title polling, side layout, transitions, trackpad momentum, vertical scrolling, editor/code/pinch exclusions, touch swipe, modal exclusion, reduced motion and mobile layout')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    check_navigation()

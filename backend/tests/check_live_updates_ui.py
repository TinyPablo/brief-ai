"""Browser checks for immediate snapshot updates and polling recovery; fake models only."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def check():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(ROOT / 'frontend/public')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    chats, receipts, reads, errors = {}, {}, {}, []
    stamp = '2026-10-31T12:00:00Z'
    def complete(cid, answer):
        chat = chats[cid]
        chat['messages'].append(dict(id=str(uuid4()), role='assistant', content=answer, model='gemini',
            images=[], charts=[], created_at=stamp, input_tokens=1, output_tokens=1, cost_usd=.001, duration_ms=1))
        chat['job']['status'] = 'completed'
    def route_api(route):
        path = route.request.url.split('/api', 1)[1]
        if path == '/session':
            data = {'authenticated': True}
        elif path == '/config':
            data = {'models': [dict(id='gemini', label='Gemini', provider='google', input=1, output=1, context_window=1000000)],
                    'default': 'gemini', 'conversation_events': True}
        elif path == '/conversations' and route.request.method == 'POST':
            body = route.request.post_data_json
            cid = str(uuid4())
            chats[cid] = dict(id=cid, title=body['prompt'], title_status='completed', revision=0,
                messages=[dict(id=str(uuid4()), role='user', content=body['prompt'], model='gemini', images=[], created_at=stamp)],
                job={'status': 'queued'})
            data = receipts[body['request_id']] = {'id': cid}
        elif path.startswith('/conversation-requests/'):
            data = receipts[path.rsplit('/', 1)[1]]
        elif path.startswith('/conversations/'):
            cid = path.rsplit('/', 1)[1]
            reads[cid] = reads.get(cid, 0) + 1
            data = chats[cid]
        else:
            raise AssertionError(path)
        route.fulfill(content_type='application/json', body=json.dumps(data))
    try:
        with sync_playwright() as p:
            browser = p.webkit.launch(headless=True) if os.environ.get('BRIEF_TEST_BROWSER') == 'webkit' else p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
            context = browser.new_context(viewport={'width': 1360, 'height': 1000})
            context.add_init_script('''window.sources=[];class FakeSource extends EventTarget {
              constructor(url){super();if(url!=='/api/conversation-events')throw Error('Unexpected URL');sources.push(this);setTimeout(()=>this.emit('ready',{}),50);}
              emit(type,data){this.dispatchEvent(new MessageEvent(type,{data:JSON.stringify(data)}));}
              close(){this.closed=true;}
            }window.EventSource=FakeSource;''')
            context.route('**/api/**', route_api)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.wait_for_function('window.BriefConversationEvents?.connected()')
            active = page.locator('[data-conversation-view]:not([hidden])')
            def send(text):
                active.get_by_placeholder('Ask anything…').fill(text)
                active.get_by_role('button', name='brief', exact=True).click()
                expect(active.get_by_text('Queued…', exact=True)).to_be_visible()
                return active.get_attribute('data-conversation-id')
            first = send('First')
            page.get_by_role('button', name='New conversation', exact=True).click()
            second = send('Second')
            assert page.evaluate('sources.length') == 1
            # A cached hidden view receives its result without switching the active chat.
            complete(first, 'Background answer')
            page.evaluate('(id) => sources[0].emit("changed",{id})', first)
            expect(page.locator(f'[data-conversation-id="{first}"] .md')).to_have_text('Background answer')
            expect(active).to_have_attribute('data-conversation-id', second)
            expect(active.locator('.md')).to_have_count(0)
            # A completion event replaces the 1.2-second response polling delay.
            complete(second, 'Immediate answer')
            page.evaluate('''(id) => {
              window.deliveredMs=null;const start=performance.now();
              const observer=new MutationObserver(()=>{if(document.querySelector('[data-conversation-view]:not([hidden]) .md')){deliveredMs=performance.now()-start;observer.disconnect();}});
              observer.observe(document.body,{childList:true,subtree:true});
              for(let i=0;i<10;i++)sources[0].emit('changed',{id});
            }''', second)
            expect(active.locator('.md')).to_have_text('Immediate answer', timeout=1000)
            latency = page.evaluate('deliveredMs')
            assert latency < 800, latency
            assert reads[second] <= 4, reads
            # With the stream disconnected, the existing polling path still completes.
            page.get_by_role('button', name='New conversation', exact=True).click()
            third = send('Fallback')
            page.evaluate('sources[0].emit("error",{})')
            page.wait_for_function('!BriefConversationEvents.connected()')
            complete(third, 'Polling recovered')
            expect(active.locator('.md')).to_have_text('Polling recovered', timeout=4000)
            # Reconnect snapshots repair a missed event, including after refresh.
            page.get_by_role('button', name='New conversation', exact=True).click()
            fourth = send('Reconnect')
            complete(fourth, 'Recovered snapshot')
            page.evaluate('sources[0].emit("ready",{})')
            expect(active.locator('.md')).to_have_text('Recovered snapshot', timeout=1000)
            page.reload()
            expect(active.locator('.md')).to_have_text('Recovered snapshot')
            page.wait_for_function('BriefConversationEvents.connected()')
            assert page.evaluate('sources.length') == 1
            assert not errors, errors
            browser.close()
            print(f'PASS: event-to-DOM {latency:.1f} ms, one connection/page, hidden chats, burst coalescing, polling fallback and refresh recovery')
    finally:
        server.shutdown()


if __name__ == '__main__':
    check()

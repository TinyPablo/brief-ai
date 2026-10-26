"""Browser checks for charts in saved conversations and anonymous share pages."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from uuid import uuid4

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith('/s/'):
            self.path = '/index.html'
        super().do_GET()


def check_ui():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(ROOT/'frontend/public')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cid = str(uuid4())
    stamp = '2026-10-27T12:00:00Z'
    def chart(kind):
        return dict(version=1, type=kind, title={'bar':'Spending','line':'Growth','scatter':'Relationship'}[kind],
                    x_label='Month' if kind=='bar' else 'Time (years)', y_label='Amount (PLN)',
                    source='example', assumptions='Illustrative data only.',
                    series=[dict(name='Actual', points=[dict(x=x if kind!='bar' else str(x), y=y) for x,y in [(1,-100),(2,1500),(3,900)]]),
                            dict(name='Budget', points=[dict(x=x if kind!='bar' else str(x), y=1300) for x in [1,2,3]])])
    charts = [chart(kind) for kind in ('bar','line','scatter')]
    messages = [dict(id=str(uuid4()), role=role, content=text, model='gemini', model_label='Gemini', created_at=stamp,
                     charts=charts if role=='assistant' else [], images=[], input_tokens=10, output_tokens=5, cost_usd=.001, duration_ms=100)
                for role,text in [('user','Compare spending'),('assistant','A visual comparison.')]]
    def route_api(route):
        path = route.request.url.split('/api',1)[1]
        if path=='/session': data={'authenticated':True}
        elif path=='/config': data={'models':[dict(id='gemini',label='Gemini',provider='google',input=1,output=2,context_window=1000000,supports_images=True)],'default':'gemini','usd_pln':4}
        elif path.startswith('/conversations?'): data={'items':[dict(id=cid,prompt_preview='Compare spending',created_at=stamp,message_count=2,status='completed')]}
        elif path=='/conversations/'+cid: data=dict(id=cid,title='Spending',context='',revision=0,messages=messages,job={'status':'completed'})
        elif path.startswith('/history?'): data={'items':[]}
        elif path=='/share/c_bad-chart': data=dict(answer='Saved answer.', charts=[{'version':2}], model_label='Gemini', created_at=stamp, prompt=None)
        elif path.startswith('/share/'): data=dict(messages=messages,title='Spending',scope='conversation')
        else: data={}
        route.fulfill(content_type='application/json',body=json.dumps(data))
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
            page = browser.new_page(viewport={'width':1280,'height':1000})
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.route('**/api/**',route_api)
            url = 'http://127.0.0.1:'+str(server.server_port)
            page.goto(url)
            page.get_by_role('button',name='history',exact=True).click()
            page.get_by_role('button',name='Compare spending').click()
            expect(page.locator('.answer-chart')).to_have_count(3)
            page.reload()
            expect(page.locator('.answer-chart')).to_have_count(3)
            first = page.locator('.answer-chart').first
            first.get_by_role('button',name='Budget',exact=True).click()
            expect(first.get_by_role('button',name='Budget',exact=True)).to_have_attribute('aria-pressed','false')
            first.get_by_role('button',name='View data').click()
            expect(first.locator('tbody tr')).to_have_count(6)
            first.get_by_role('button',name='Actual',exact=True).focus()
            page.keyboard.press('Enter')
            expect(first.get_by_text('Select a series to display')).to_be_visible()
            first.get_by_role('button',name='Actual',exact=True).click()
            first.locator('svg').hover(position={'x':180,'y':120})
            expect(first.locator('.chart-detail strong')).to_be_visible()
            page.goto(url+'/s/c_chart-test')
            expect(page.locator('.answer-chart')).to_have_count(3)
            expect(page.get_by_text('Illustrative data · Illustrative data only.')).to_have_count(3)
            for width in (1280,390,320):
                page.set_viewport_size({'width':width,'height':900})
                page.wait_for_timeout(100)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Overflow at {width}px'
                assert page.locator('.answer-chart svg').first.evaluate('(svg)=>svg.getBoundingClientRect().width') > 180
            page.screenshot(path='/private/tmp/brief-charts-mobile.png',full_page=True)
            page.set_viewport_size({'width':1280,'height':1100})
            page.screenshot(path='/private/tmp/brief-charts-desktop.png',full_page=True)
            page.goto(url+'/s/c_bad-chart')
            expect(page.get_by_text('This chart is unavailable.')).to_be_visible()
            expect(page.get_by_text('Saved answer.')).to_be_visible()
            assert not errors, errors
            browser.close()
        print('PASS: saved conversation, reload, anonymous sharing, three chart types, negative values, series toggles, keyboard, value inspection, data access, 320px/mobile/desktop layout')
    finally:
        server.shutdown()
        server.server_close()


if __name__=='__main__':
    check_ui()

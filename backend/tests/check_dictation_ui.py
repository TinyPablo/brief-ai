"""Exercise real microphone worklets with synthetic audio and a fake Soniox socket."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
FAKE_SOCKET = """
window.sockets = [];
window.tracks = [];
const capture = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getUserMedia = async (...args) => {
  const stream = await capture(...args);
  window.tracks.push(...stream.getTracks());
  return stream;
};
class FakeSocket {
  static OPEN = 1;
  constructor(url, protocols) {
    if (url !== 'wss://stt-rt.soniox.com/transcribe-websocket') throw Error('Unexpected provider');
    if (protocols[0] !== 'soniox-api-key' || protocols[1] !== 'temporary-test') throw Error('Invalid credential');
    this.readyState = 0; this.bufferedAmount = 0; this.frames = []; this.binary = 0;
    window.sockets.push(this);
    setTimeout(() => { this.readyState = 1; this.onopen?.(); }, 20);
  }
  send(data) {
    if (typeof data !== 'string') { this.binary++; return; }
    this.frames.push(data);
    if (data === '' && !window.holdFinish) setTimeout(() => this.emit({finished: true}), 80);
  }
  emit(message) { this.onmessage?.({data: JSON.stringify(message)}); }
  close() { this.readyState = 3; this.onclose?.(); }
}
window.WebSocket = FakeSocket;
"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def check():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'frontend/public')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    errors = []
    sessions = []
    chat = dict(id='chat', title='Test chat', revision=0, messages=[
        dict(id='question', role='user', content='Original question', model='gemini', images=[], created_at='2026-10-27T12:00:00Z'),
        dict(id='answer', role='assistant', content='Original answer', model='gemini', images=[], created_at='2026-10-27T12:00:00Z'),
    ], job={'status': 'completed'})
    for message in chat['messages']:
        message.update(input_tokens=10, output_tokens=5, duration_ms=100, cost_usd=0.001)

    def route_api(route):
        path = route.request.url.split('/api', 1)[1]
        if path == '/session':
            data = {'authenticated': True}
        elif path == '/config':
            data = {'models': [dict(id='gemini', label='Gemini', provider='google', input=1, output=2, context_window=1000000)], 'default': 'gemini', 'dictation': {'enabled': True}}
        elif path == '/dictation/session':
            assert route.request.headers['x-brief-dictation'] == '1'
            sessions.append(True)
            data = {'api_key': 'temporary-test'}
        elif path.startswith('/conversations?'):
            data = {'items': [dict(id='chat', title='Test chat', prompt_preview='Question', created_at='2026-10-27T12:00:00Z', message_count=2, status='completed')]}
        elif path == '/conversations/chat':
            data = chat
        elif path.startswith('/history?'):
            data = {'items': []}
        else:
            raise AssertionError(f'Unexpected API request: {path}')
        route.fulfill(content_type='application/json', body=json.dumps(data))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True, args=['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'])
            context = browser.new_context(viewport={'width': 1360, 'height': 1000}, permissions=['microphone'])
            context.add_init_script(FAKE_SOCKET)
            context.route('**/api/**', route_api)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.set_default_timeout(10000)
            page.goto(f'http://127.0.0.1:{server.server_port}')
            active = page.locator('[data-conversation-view]:not([hidden])')
            prompt = active.get_by_placeholder('Ask anything…')
            prompt.fill('Keep this draft.')
            active.get_by_role('button', name='Start dictation').click()
            expect(active.get_by_text('Listening · PL / EN')).to_be_visible()
            expect(prompt).to_be_disabled()
            expect(active.get_by_role('button', name='brief', exact=True)).to_be_disabled()
            page.wait_for_function('sockets.at(-1).binary > 0')
            config = page.evaluate('JSON.parse(sockets.at(-1).frames[0])')
            assert config['audio_format'] == 'pcm_s16le' and config['sample_rate'] == 16000
            assert config['language_hints'] == ['pl', 'en'] and 'api_key' not in config
            page.evaluate("sockets.at(-1).emit({tokens:[{text:'Cześć ',is_final:true},{text:'Docker',is_final:false}]})")
            expect(prompt).to_have_value('Keep this draft. Cześć Docker')
            page.evaluate("sockets.at(-1).emit({tokens:[{text:'Docker i React.',is_final:false}]})")
            expect(prompt).to_have_value('Keep this draft. Cześć Docker i React.')
            page.evaluate("sockets.at(-1).emit({tokens:[{text:'Docker i React.',is_final:true},{text:'<end>',is_final:true}]})")
            expect(prompt).to_have_value('Keep this draft. Cześć Docker i React.')
            expect(active.get_by_role('button', name='Stop dictation')).to_be_visible()
            page.evaluate('window.holdFinish = true')
            active.get_by_role('button', name='Stop dictation').click()
            expect(active.get_by_text('Finishing transcription…')).to_be_visible()
            expect(prompt).to_be_disabled()
            page.wait_for_function("tracks.every(t => t.readyState === 'ended') && sockets.at(-1).frames.includes('')")
            page.evaluate("sockets.at(-1).emit({tokens:[{text:' Koniec.',is_final:true}],finished:true})")
            expect(prompt).to_be_enabled()
            expect(prompt).to_have_value('Keep this draft. Cześć Docker i React. Koniec.')
            prompt.fill('Edited draft.')
            active.get_by_role('button', name='Start dictation').click()
            expect(active.get_by_text('Listening · PL / EN')).to_be_visible()
            page.evaluate("sockets.at(-1).emit({tokens:[{text:'scapular push-ups',is_final:false}]}); sockets.at(-1).close()")
            expect(prompt).to_have_value('Edited draft. scapular push-ups')
            expect(prompt).to_be_enabled()
            expect(active.get_by_role('button', name='Resume dictation')).to_be_visible()
            active.get_by_role('button', name='Resume dictation').click()
            expect(active.get_by_text('Listening · PL / EN')).to_be_visible()
            page.evaluate("sockets.at(-1).emit({tokens:[{text:' i 1RM.',is_final:true}]}); window.holdFinish = false")
            active.get_by_role('button', name='Stop dictation').click()
            expect(prompt).to_have_value('Edited draft. scapular push-ups i 1RM.')
            expect(prompt).to_be_enabled()
            # Moving to a neighboring workspace stops capture and preserves this draft.
            active.get_by_role('button', name='Start dictation').click()
            expect(active.get_by_text('Listening · PL / EN')).to_be_visible()
            page.get_by_role('button', name='New conversation', exact=True).click()
            page.wait_for_function("tracks.every(t => t.readyState === 'ended')")
            page.locator('[aria-label^="Previous conversation:"]').click()
            expect(prompt).to_have_value('Edited draft. scapular push-ups i 1RM.')
            expect(active.get_by_role('button', name='Start dictation')).to_be_visible()
            # Navigation stops the microphone while retaining the draft.
            active.get_by_role('button', name='Start dictation').click()
            expect(active.get_by_text('Listening · PL / EN')).to_be_visible()
            page.get_by_role('button', name='history', exact=True).click()
            page.wait_for_function("tracks.every(t => t.readyState === 'ended')")
            page.get_by_role('button', name='Test chat').click()
            active.get_by_role('button', name='Edit', exact=True).first.click()
            editor = active.get_by_role('textbox', name='Edit message')
            editor.fill('Editing question.')
            editor_box = active.locator('.message-editor')
            editor_box.get_by_role('button', name='Start dictation').click()
            expect(editor_box.get_by_text('Listening · PL / EN')).to_be_visible()
            expect(editor).to_be_disabled()
            expect(editor_box.get_by_role('button', name='Save', exact=True)).to_be_disabled()
            page.evaluate("sockets.at(-1).emit({tokens:[{text:' PostgreSQL.',is_final:true}]})")
            editor_box.get_by_role('button', name='Stop dictation').click()
            expect(editor).to_have_value('Editing question. PostgreSQL.')
            expect(editor).to_be_enabled()
            # Leaving cancels the entire earlier-message edit.
            editor_box.get_by_role('button', name='Start dictation').click()
            expect(editor_box.get_by_text('Listening · PL / EN')).to_be_visible()
            page.get_by_role('button', name='history', exact=True).click()
            page.wait_for_function("tracks.every(t => t.readyState === 'ended')")
            page.get_by_role('button', name='Test chat').click()
            expect(editor).to_have_count(0)
            expect(active.get_by_text('Original question', exact=True)).to_be_visible()
            active.get_by_role('button', name='Edit', exact=True).nth(1).click()
            expect(active.locator('.message-editor').get_by_role('button', name='Start dictation')).to_have_count(0)
            active.get_by_role('button', name='Cancel editing').click()
            page.set_viewport_size({'width': 390, 'height': 844})
            active.get_by_placeholder('Follow up…').fill('Mobile prompt')
            active.get_by_role('button', name='Start dictation').scroll_into_view_if_needed()
            page.screenshot(path='/private/tmp/brief-dictation-mobile.png', full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            assert len(sessions) == 7, len(sessions)
            # The same reducer handles changing partials without duplicating final text.
            assert page.evaluate("""() => {
              const text = {stable:'', partial:''};
              BriefDictation.reduce(text, {tokens:[{text:'hello ',is_final:true},{text:'wor',is_final:false}]});
              BriefDictation.reduce(text, {tokens:[{text:'world',is_final:false}]});
              BriefDictation.reduce(text, {tokens:[]});
              return text.stable + text.partial;
            }""") == 'hello world'
            # Verify actual PCM output for both common microphone sample rates.
            assert page.evaluate("""async () => {
              const script = await (await fetch('/dictation-worklet.js')).text();
              for (const rate of [44100, 48000]) {
                let Capture;
                const chunks = [];
                class Processor { constructor() { this.port = {postMessage: m => {if(m.bytes) chunks.push(m.bytes);}}; } }
                new Function('AudioWorkletProcessor','sampleRate','registerProcessor',script)(Processor,rate,(_, cls) => Capture = cls);
                const processor = new Capture();
                for (let n = 0; n < rate; n += 128) processor.process([[new Float32Array(Math.min(128,rate-n)).fill(.5)]]);
                processor.port.onmessage({data:{type:'flush'}});
                if (chunks.reduce((n,b) => n+b.byteLength,0) !== 32000) return false;
                for (const bytes of chunks) {
                  const view = new DataView(bytes);
                  for(let i=0;i<bytes.byteLength;i+=2) if(view.getInt16(i,true)!==16384) return false;
                }
              }
              return true;
            }""")
            # Cancellation before microphone permission resolves must stop late tracks.
            page.evaluate("""() => {
              window.engine = new BriefDictation.Session({onText:()=>{},onState:s=>window.engineState=s});
              engine.start('base'); engine.stop();
            }""")
            page.wait_for_function("tracks.every(t => t.readyState === 'ended')")
            assert page.evaluate('engineState.state') == 'idle'
            # Permission errors must release the owner and allow an explicit retry.
            page.evaluate("""async () => {
              const original = navigator.mediaDevices.getUserMedia;
              navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('Denied','NotAllowedError'));
              engine = new BriefDictation.Session({onText:t=>window.engineText=t,onState:s=>window.engineState=s});
              await engine.start('base');
              navigator.mediaDevices.getUserMedia = original;
            }""")
            assert page.evaluate('engineState.state') == 'error'
            assert 'denied' in page.evaluate('engineState.error')
            page.evaluate("""async () => {
              engine = new BriefDictation.Session({onText:t=>window.engineText=t,onState:s=>window.engineState=s});
              await engine.start('base');
            }""")
            page.wait_for_function("engineState.state === 'recording'")
            page.evaluate("sockets.at(-1).emit({tokens:[{text:' visible',is_final:false}]}); sockets.at(-1).emit({error_code:402,error_type:'organization_balance_exhausted'})")
            assert page.evaluate('engineText') == 'base visible'
            assert 'balance' in page.evaluate('engineState.error')
            page.wait_for_function("tracks.every(t => t.readyState === 'ended')")
            # A provider that never acknowledges Stop must not leave the field locked.
            page.evaluate("""async () => {
              window.holdFinish = true;
              engine = new BriefDictation.Session({onText:t=>window.engineText=t,onState:s=>window.engineState=s});
              await engine.start('base');
            }""")
            page.wait_for_function("engineState.state === 'recording'")
            page.evaluate("sockets.at(-1).emit({tokens:[{text:' provisional',is_final:false}]}); engine.stop()")
            page.wait_for_function("engineState.state === 'error'", timeout=10000)
            assert page.evaluate('engineText') == 'base provisional'
            assert page.evaluate("tracks.every(t => t.readyState === 'ended')")
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
    print('Dictation UI: streaming, worklet audio, finalization, failure/resume, navigation, editing and mobile layout passed.')


if __name__ == '__main__':
    check()

"""Check native browser storage, including files, reloads and per-tab isolation."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def check_storage():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'frontend/public')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
            context = browser.new_context()
            page = context.new_page()
            url = f'http://127.0.0.1:{server.server_port}/conversation-sessions.js'
            page.goto(url)
            page.add_script_tag(url=url)
            result = page.evaluate('''async () => {
              const { WorkspaceStorage, freshView } = BriefSessions;
              window.failures = [];
              window.storage = new WorkspaceStorage(() => failures.push('failed'));
              const initial = await storage.restore();
              const view = initial.views[0];
              view.prompt = 'Unsent text'; view.model = 'test-model'; view.scroll = 120; view.atBottom = false;
              view.images = [{id:'image-1', name:'photo.png', mediaType:'image/png', sizeBytes:5,
                width:1, height:1, base64:btoa('image'), thumb:null}];
              view.conversation = {messages:[{role:'user',content:'saved'}]};
              storage.setOrder([view], view.key);
              await storage.save(view);
              return { key:view.key, session:storage.meta.id };
            }''')
            page.reload()
            page.add_script_tag(url=url)
            restored = page.evaluate('''async () => {
              window.storage = new BriefSessions.WorkspaceStorage(() => {});
              window.restored = await storage.restore();
              const view = restored.views[0];
              return {prompt:view.prompt, model:view.model, scroll:view.scroll, atBottom:view.atBottom,
                image:atob(view.images[0].base64), name:view.images[0].name,
                cached:view.conversation.messages[0].content, session:storage.meta.id};
            }''')
            assert restored == dict(prompt='Unsent text', model='test-model', scroll=120, atBottom=False,
                                    image='image', name='photo.png', cached='saved', session=result['session'])
            other = context.new_page()
            other.goto(url)
            other.add_script_tag(url=url)
            isolated = other.evaluate('''async () => {const s=new BriefSessions.WorkspaceStorage(()=>{});const r=await s.restore();return {id:s.meta.id,prompt:r.views[0].prompt,images:r.views[0].images.length};}''')
            assert isolated['id'] != result['session']
            assert isolated['prompt'] == '' and isolated['images'] == 0
            duplicated = context.new_page()
            duplicated.goto(url)
            duplicated.evaluate('(meta) => sessionStorage.setItem("briefai_workspace_v1", meta)', page.evaluate('sessionStorage.getItem("briefai_workspace_v1")'))
            duplicated.add_script_tag(url=url)
            forked = duplicated.evaluate('''async () => {const s=new BriefSessions.WorkspaceStorage(()=>{});const r=await s.restore();return {id:s.meta.id,prompt:r.views[0].prompt,images:r.views[0].images.length};}''')
            assert forked['id'] != result['session']
            assert forked['prompt'] == '' and forked['images'] == 0
            assert page.evaluate('''async () => {
              await storage.remove(restored.active);
              const db=await storage.db;
              return await new Promise((resolve,reject)=>{const tx=db.transaction('attachments');const r=tx.objectStore('attachments').count();r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
            }''') == 0
            browser.close()
            print('PASS: IndexedDB drafts, binary attachments, reload, snapshot, model, scroll, tab/duplicate isolation and attachment cleanup')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    check_storage()

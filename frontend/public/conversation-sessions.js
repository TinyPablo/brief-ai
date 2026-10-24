/* Local workspace storage. Sent messages remain authoritative on the server. */
(() => {
  const META_KEY = 'briefai_workspace_v1';
  const DB_NAME = 'briefai_workspaces';
  const TTL = 7 * 24 * 60 * 60 * 1000;
  const freshView = (conversationId = null, model = '') => ({
    key: crypto.randomUUID(), conversationId, model, prompt: '', images: [],
    conversation: null, scroll: 0, atBottom: true, unread: false, pendingRequest: null,
  });
  const dataUrl = (blob) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
  function blobFromBase64(encoded, type) {
    const bytes = Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0));
    return new Blob([bytes], { type });
  }
  function attachment(image) {
    return {
      id: image.id, name: image.name, mediaType: image.mediaType, sizeBytes: image.sizeBytes,
      width: image.width, height: image.height,
      blob: image.blob || blobFromBase64(image.base64, image.mediaType),
      thumb: image.thumb ? blobFromBase64(image.thumb.base64, image.thumb.mediaType) : null,
    };
  }
  async function restoreImage(image) {
    const previewUrl = await dataUrl(image.blob);
    const thumbUrl = image.thumb ? await dataUrl(image.thumb) : null;
    return { ...image, previewUrl, base64: previewUrl.split(',')[1],
      thumb: thumbUrl ? { base64: thumbUrl.split(',')[1], mediaType: image.thumb.type } : null };
  }
  function openDatabase() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = () => {
        for (const name of ['views', 'attachments', 'snapshots', 'sessions']) request.result.createObjectStore(name);
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error('Local storage is blocked.'));
    });
  }
  const resultOf = (request) => new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  const finished = (tx) => new Promise((resolve, reject) => {
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error || new Error('Local storage write failed.'));
    tx.onabort = () => reject(tx.error || new Error('Local storage write was interrupted.'));
  });

  class WorkspaceStorage {
    constructor(onError) {
      this.onError = onError;
      this.previous = new Map();
      this.removed = new Set();
      this.queue = Promise.resolve();
      try { this.meta = JSON.parse(sessionStorage.getItem(META_KEY)); } catch {}
      if (!this.meta || this.meta.version !== 1 || !Array.isArray(this.meta.views) || !this.meta.views.length || typeof this.meta.id !== 'string' || this.meta.views.some((view) => !view || typeof view.key !== 'string')) {
        let legacy = null;
        try { legacy = sessionStorage.getItem('briefai_active_conversation'); } catch {}
        const view = freshView(legacy);
        try { const pending = sessionStorage.getItem('briefai_pending_request'); if (pending) view.pendingRequest = { id: pending }; } catch {}
        this.meta = { version: 1, id: crypto.randomUUID(), active: view.key, views: [view] };
      }
      this.db = openDatabase();
      this.db.catch(() => this.onError());
      this.owner = crypto.randomUUID();
      this.channel = null;
      this.claimed = false;
      this.onPageHide = () => { this.channel?.close(); this.channel = null; };
      this.onPageShow = (event) => { if (event.persisted) this.openChannel(); };
      window.addEventListener('pagehide', this.onPageHide);
      window.addEventListener('pageshow', this.onPageShow);
    }
    openChannel() {
      if (typeof BroadcastChannel === 'undefined' || this.channel) return;
      this.channel = new BroadcastChannel('briefai_workspace_owners_v1');
      this.channel.onmessage = ({ data }) => {
        if (data.id !== this.meta.id || data.owner === this.owner) return;
        if (data.type === 'probe' && (this.claimed || this.owner < data.owner)) {
          this.channel.postMessage({ type: 'occupied', id: this.meta.id, owner: this.owner, target: data.owner });
        }
        if (data.type === 'occupied' && data.target === this.owner) this.occupied = true;
      };
    }
    async claim() {
      this.openChannel();
      if (this.channel) {
        this.channel.postMessage({ type: 'probe', id: this.meta.id, owner: this.owner });
        await new Promise((resolve) => setTimeout(resolve, 120));
        if (this.occupied) {
          const view = freshView();
          this.meta = { version: 1, id: crypto.randomUUID(), active: view.key, views: [view] };
        }
      }
      this.claimed = true;
    }
    async restore() {
      await this.claim();
      let views;
      try {
        const db = await this.db;
        const tx = db.transaction(['views', 'attachments', 'snapshots']);
        // Queue every read before awaiting: IndexedDB transactions close when idle.
        const reads = this.meta.views.map((entry) => ({ entry,
          draft: resultOf(tx.objectStore('views').get([this.meta.id, entry.key])),
          snapshot: resultOf(tx.objectStore('snapshots').get([this.meta.id, entry.key])),
        }));
        const drafts = await Promise.all(reads.map(async ({ entry, draft, snapshot }) => ({ entry, draft: await draft, snapshot: await snapshot })));
        const imageTx = db.transaction('attachments');
        const withImages = drafts.map((row) => ({ ...row, images: (row.draft?.imageIds || []).map((id) =>
          resultOf(imageTx.objectStore('attachments').get([this.meta.id, row.entry.key, id]))) }));
        views = await Promise.all(withImages.map(async ({ entry, draft, snapshot, images }) => ({
          ...freshView(entry.conversationId, entry.model), ...entry, ...draft,
          conversation: snapshot || null,
          images: await Promise.all((await Promise.all(images)).filter(Boolean).map(restoreImage)),
        })));
      } catch {
        this.onError();
        views = this.meta.views.map((entry) => ({ ...freshView(entry.conversationId, entry.model), ...entry }));
      }
      const active = views.some((v) => v.key === this.meta.active) ? this.meta.active : views[0].key;
      this.setOrder(views, active);
      this.cleanup().catch(() => this.onError());
      return { views, active };
    }
    setOrder(views, active) {
      this.meta = { ...this.meta, active, views: views.map(({ key, conversationId, model }) => ({ key, conversationId, model })) };
      try { sessionStorage.setItem(META_KEY, JSON.stringify(this.meta)); } catch { this.onError(); }
    }
    save(view) {
      if (this.removed.has(view.key)) return this.queue;
      const previous = this.previous.get(view.key);
      this.previous.set(view.key, view);
      const sessionId = this.meta.id;
      this.queue = this.queue.catch(() => {}).then(async () => {
        const db = await this.db;
        const tx = db.transaction(['views', 'attachments', 'snapshots', 'sessions'], 'readwrite');
        const done = finished(tx);
        const { images, conversation, ...draft } = view;
        tx.objectStore('views').put({ ...draft, imageIds: images.map((im) => im.id) }, [sessionId, view.key]);
        const oldIds = new Set(previous?.images?.map((im) => im.id));
        const newIds = new Set(images.map((im) => im.id));
        for (const image of images) {
          if (!oldIds.has(image.id)) tx.objectStore('attachments').put(attachment(image), [sessionId, view.key, image.id]);
        }
        for (const id of oldIds) if (!newIds.has(id)) tx.objectStore('attachments').delete([sessionId, view.key, id]);
        if (!previous || previous.conversation !== conversation) {
          tx.objectStore('snapshots').put(conversation, [sessionId, view.key]);
        }
        tx.objectStore('sessions').put({ touched: Date.now() }, sessionId);
        await done;
      });
      this.queue.catch(() => { this.previous.delete(view.key); this.onError(); });
      return this.queue;
    }
    remove(key) {
      this.removed.add(key);
      this.previous.delete(key);
      const sessionId = this.meta.id;
      this.queue = this.queue.catch(() => {}).then(async () => {
        const db = await this.db;
        const tx = db.transaction(['views', 'attachments', 'snapshots'], 'readwrite');
        const done = finished(tx);
        tx.objectStore('views').delete([sessionId, key]);
        tx.objectStore('snapshots').delete([sessionId, key]);
        tx.objectStore('attachments').delete(IDBKeyRange.bound([sessionId, key], [sessionId, key, []]));
        await done;
      });
      this.queue.catch(() => this.onError());
      return this.queue;
    }
    dispose() {
      this.channel?.close();
      window.removeEventListener('pagehide', this.onPageHide);
      window.removeEventListener('pageshow', this.onPageShow);
      this.queue.catch(() => {}).then(() => this.db).then((db) => db.close()).catch(() => {});
    }
    async cleanup() {
      const db = await this.db;
      const tx = db.transaction(['views', 'attachments', 'snapshots', 'sessions'], 'readwrite');
      const done = finished(tx);
      const sessions = tx.objectStore('sessions');
      const cursor = sessions.openCursor();
      cursor.onsuccess = () => {
        const row = cursor.result;
        if (!row) return;
        if (row.key !== this.meta.id && row.value.touched < Date.now() - TTL) {
          const range = IDBKeyRange.bound([row.key], [row.key, []]);
          for (const name of ['views', 'attachments', 'snapshots']) tx.objectStore(name).delete(range);
          row.delete();
        }
        row.continue();
      };
      await done;
    }
  }
  window.BriefSessions = { WorkspaceStorage, freshView };
})();

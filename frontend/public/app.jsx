const { useState, useEffect, useRef, useMemo, useCallback } = React;

function api(path, opts = {}) {
  return fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    ...opts,
  });
}

function fmtDuration(ms) {
  if (ms < 1000) return ms + ' ms';
  return (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + ' s';
}

function zl(value) {
  const d = Math.abs(value) < 0.1 ? 4 : 2;
  return parseFloat(value.toFixed(d)) + ' zł';
}

function fmtTime(iso) {
  try {
    return new Date(iso).toLocaleString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  } catch (e) {
    return iso;
  }
}

const SETTINGS_KEY = 'briefai_settings';
const DEFAULT_SETTINGS = {
  nowOn: true,
  locOn: true,
  locText: 'Bielsko-Biała',
  userOn: false,
  userText: '',
};

function loadSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    return raw ? { ...DEFAULT_SETTINGS, ...JSON.parse(raw) } : { ...DEFAULT_SETTINGS };
  } catch (e) {
    return { ...DEFAULT_SETTINGS };
  }
}

function ordinal(n) {
  const s = n % 100;
  if (s >= 11 && s <= 13) return n + 'th';
  return n + (['th', 'st', 'nd', 'rd'][n % 10] || 'th');
}

function formatDate(d) {
  const month = d.toLocaleString('en-US', { month: 'long' });
  return ordinal(d.getDate()) + ' ' + month + ' ' + d.getFullYear();
}

function buildContext(settings) {
  const lines = [];
  if (settings.nowOn) {
    const now = new Date();
    const time = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    lines.push('now: ' + formatDate(now) + ', ' + time);
  }
  if (settings.locOn && settings.locText.trim()) {
    lines.push('user is currently in ' + settings.locText.trim());
  }
  if (settings.userOn && settings.userText.trim()) {
    lines.push('user data: ' + settings.userText.trim());
  }
  return lines.join('\n');
}

// The composed text is the server's job now: /api/generate takes `prompt`,
// `context` and `brief` separately and joins them on the way to the model, so
// the stored prompt stays exactly what was typed. This local copy exists only
// to size the live cost estimate.
function estimateChars(prompt, settings, brief) {
  const ctx = buildContext(settings);
  return (ctx ? ctx.length + 2 : 0) + (brief ? 12 : 0) + prompt.length;
}

if (window.markedKatex) {
  marked.use(window.markedKatex({ throwOnError: false, nonStandard: true, output: 'html' }));
}
marked.setOptions({ gfm: true, breaks: true });

function addCopyButton(pre) {
  if (pre.querySelector('.copy-btn')) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'copy-btn';
  btn.textContent = 'Copy';
  btn.addEventListener('click', () => {
    const code = pre.querySelector('code');
    const text = code ? code.innerText : pre.innerText;
    navigator.clipboard.writeText(text).then(() => {
      btn.textContent = 'Copied';
      setTimeout(() => { btn.textContent = 'Copy'; }, 1200);
    });
  });
  pre.appendChild(btn);
}

function Markdown({ text }) {
  const ref = useRef(null);
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(text || '')), [text]);
  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    root.querySelectorAll('pre code').forEach((el) => {
      try { hljs.highlightElement(el); } catch (e) { /* noop */ }
    });
    root.querySelectorAll('pre').forEach(addCopyButton);
  }, [html]);
  return <div ref={ref} className="md" dangerouslySetInnerHTML={{ __html: html }} />;
}

function Meta({ result, rate, onCopy, copied }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[11px] text-muted border-b border-edge pb-3 mb-4">
      <span className="text-accent font-bold">{result.model_label}</span>
      <span>{fmtDuration(result.duration_ms)}</span>
      <span>{result.input_tokens.toLocaleString()}/{result.output_tokens.toLocaleString()} tok</span>
      <span className="glow text-accent">{zl(result.cost_usd * rate)}</span>
      {onCopy && (
        <button
          onClick={onCopy}
          className="ml-auto text-muted hover:text-white transition-colors underline underline-offset-2"
        >
          {copied ? 'copied' : 'copy'}
        </button>
      )}
    </div>
  );
}

function Login({ onSuccess }) {
  const [digits, setDigits] = useState(['', '', '', '', '', '']);
  const [error, setError] = useState('');
  const [cooldown, setCooldown] = useState(0);
  const [busy, setBusy] = useState(false);
  const refs = useRef([]);

  useEffect(() => { refs.current[0] && refs.current[0].focus(); }, []);

  useEffect(() => {
    if (cooldown <= 0) return;
    const t = setInterval(() => {
      setCooldown((c) => Math.max(0, +(c - 0.1).toFixed(1)));
    }, 100);
    return () => clearInterval(t);
  }, [cooldown]);

  const submit = async (code) => {
    if (code.length !== 6 || busy || cooldown > 0) return;
    setBusy(true);
    setError('');
    try {
      const res = await api('/login', { method: 'POST', body: JSON.stringify({ code }) });
      if (res.ok) { onSuccess(); return; }
      const data = await res.json();
      if (res.status === 429) {
        setCooldown(data.retry_after || 3);
        setError('Too many attempts - wait a moment.');
      } else {
        setError('Incorrect code.');
      }
      setDigits(['', '', '', '', '', '']);
      refs.current[0] && refs.current[0].focus();
    } catch (e) {
      setError('Network error.');
    } finally {
      setBusy(false);
    }
  };

  const setDigit = (i, raw) => {
    const v = raw.replace(/\D/g, '').slice(-1);
    const next = [...digits];
    next[i] = v;
    setDigits(next);
    if (v && i < 5) refs.current[i + 1] && refs.current[i + 1].focus();
    const code = next.join('');
    if (code.length === 6 && next.every((d) => d !== '')) submit(code);
  };

  const onKey = (i, e) => {
    if (e.key === 'Backspace' && !digits[i] && i > 0) {
      refs.current[i - 1] && refs.current[i - 1].focus();
    }
  };

  const onPaste = (e) => {
    const t = (e.clipboardData.getData('text') || '').replace(/\D/g, '').slice(0, 6);
    if (!t) return;
    e.preventDefault();
    const arr = ['', '', '', '', '', ''];
    for (let i = 0; i < t.length; i++) arr[i] = t[i];
    setDigits(arr);
    if (t.length === 6) submit(t);
    else refs.current[Math.min(t.length, 5)] && refs.current[Math.min(t.length, 5)].focus();
  };

  const locked = busy || cooldown > 0;

  return (
    <div className="min-h-screen grid place-items-center px-6">
      <div className="w-full max-w-sm fade-in">
        <div className="text-center mb-8">
          <h1 className="text-2xl font-mono font-bold tracking-tight">
            brief_ai<span className="text-accent glow">$</span>
          </h1>
          <p className="text-muted text-sm mt-1">Enter your authenticator code to continue</p>
        </div>

        <div className="flex justify-center gap-2.5 mb-4" onPaste={onPaste}>
          {digits.map((d, i) => (
            <input
              key={i}
              ref={(el) => (refs.current[i] = el)}
              value={d}
              disabled={locked}
              onChange={(e) => setDigit(i, e.target.value)}
              onKeyDown={(e) => onKey(i, e)}
              inputMode="numeric"
              autoComplete="off"
              type="password"
              className="pin-input w-12 h-14 text-center text-xl font-semibold bg-panel border border-edge rounded-xl outline-none transition disabled:opacity-50"
            />
          ))}
        </div>

        <div className="h-6 text-center text-sm">
          {cooldown > 0 ? (
            <span className="text-muted">Try again in {cooldown.toFixed(1)}s</span>
          ) : error ? (
            <span className="text-red-400">{error}</span>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function Modal({ title, onClose, children }) {
  const ref = useRef(null);
  useEffect(() => {
    const focus = document.activeElement;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    ref.current.showModal();
    return () => {
      document.body.style.overflow = overflow;
      if (focus instanceof HTMLElement && focus.isConnected) focus.focus();
    };
  }, []);
  return (
    <dialog ref={ref} className="deck-modal" aria-label={title} onCancel={(e) => { e.preventDefault(); onClose(); }} onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section>
        <header className="flex items-center justify-between gap-4 mb-6">
          <h2 className="deck-heading text-2xl">{title}</h2>
          <button type="button" aria-label="Close dialog" className="deck-button" onClick={onClose}>×</button>
        </header>
        {children}
      </section>
    </dialog>
  );
}

function ShareDialog({ itemId, onClose, onChanged, onUnauth, basePath = '/history/' }) {
  const [loaded, setLoaded] = useState(false);
  const [allowed, setAllowed] = useState(false);
  const [token, setToken] = useState(null);
  const [showPrompt, setShowPrompt] = useState(true);
  const [showImages, setShowImages] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    let active = true;
    api(basePath + itemId).then(async (res) => {
      if (res.status === 401) { onUnauth(); onClose(); return; }
      if (!res.ok) throw new Error('Could not load sharing settings.');
      const item = await res.json();
      if (!active) return;
      setToken(item.share_token);
      setAllowed(item.prompt_is_raw === true);
      setShowPrompt(item.share_show_prompt !== false);
      setShowImages(item.share_token ? item.share_show_images !== false : false);
      if (!item.prompt_is_raw) setError('This older entry may contain personal context and cannot be shared.');
      setLoaded(true);
    }).catch((e) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [itemId, basePath]);
  const save = async (revoke = false) => {
    setBusy(true); setError(''); setCopied(false);
    try {
      const res = await api(basePath + itemId + '/share', {
        method: revoke ? 'DELETE' : token ? 'PATCH' : 'POST',
        ...(!revoke ? { body: JSON.stringify({ show_prompt: showPrompt, show_images: showImages }) } : {}),
      });
      if (res.status === 401) { onUnauth(); onClose(); return; }
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not update sharing.');
      const next = revoke ? null : data.token;
      setToken(next); setDirty(false);
      if (onChanged) onChanged(itemId, next);
    } catch (e) { setError(e.message || 'Could not update sharing.'); }
    finally { setBusy(false); }
  };
  const url = token ? location.origin + '/s/' + token : '';
  return (
    <Modal title="Share answer" onClose={onClose}>
      <p className="text-muted text-sm mb-5">Review the answer before publishing. Anyone with the link can read it.</p>
      <fieldset disabled={!loaded || busy || !allowed} className="space-y-4">
        <label className="sharing-option"><input type="checkbox" checked={showPrompt} onChange={(e) => { setShowPrompt(e.target.checked); setDirty(true); }} /><span>Include question<small>Show the text you asked alongside the answer.</small></span></label>
        <label className="sharing-option"><input type="checkbox" checked={showImages} onChange={(e) => { setShowImages(e.target.checked); setDirty(true); }} /><span>Include images<small>Allow visitors to view and download attachments.</small></span></label>
      </fieldset>
      {token && <div className="mt-6 space-y-2">
        <label className="text-xs text-muted" htmlFor="public-link">Public link</label>
        <input id="public-link" className="deck-input w-full" value={url} readOnly onFocus={(e) => e.target.select()} />
        <button type="button" className="deck-button" onClick={async () => {
          try { await navigator.clipboard.writeText(url); setCopied(true); }
          catch { setError('Select and copy the link above. Clipboard access is unavailable.'); }
        }}>{copied ? 'Copied' : 'Copy link'}</button>
        <a className="text-accent text-sm ml-4" href={url} target="_blank" rel="noopener noreferrer">Preview ↗</a>
      </div>}
      {error && <p role="alert" className="text-red-400 text-sm mt-4">{error}</p>}
      {dirty && token && <p role="status" className="text-muted text-xs mt-4">Save changes to update the public page.</p>}
      <div className="flex flex-wrap gap-3 mt-6">
        <button type="button" className="deck-button primary" disabled={!loaded || busy || !allowed || (token && !dirty)} onClick={() => save()}>{busy ? 'Saving…' : token ? 'Save changes' : 'Publish link'}</button>
        {token && <button type="button" className="deck-button text-red-400" disabled={busy} onClick={() => save(true)}>Stop sharing</button>}
      </div>
    </Modal>
  );
}

function SwipeRow({ children, onDelete }) {
  const [revealed, setRevealed] = useState(false);
  const start = useRef(null);
  const swiped = useRef(false);
  return (
    <div className="swipe-row">
      {revealed && <button type="button" className="swipe-delete" onClick={onDelete}>Delete</button>}
      <div className="swipe-content" style={{ transform: revealed ? 'translateX(-88px)' : undefined }}
        onTouchStart={(e) => { swiped.current = false; start.current = e.touches.length === 1 ? { x: e.touches[0].clientX, y: e.touches[0].clientY } : null; }}
        onTouchEnd={(e) => {
          if (!start.current) return;
          const dx = e.changedTouches[0].clientX - start.current.x;
          const dy = e.changedTouches[0].clientY - start.current.y;
          start.current = null;
          if (Math.abs(dx) > 55 && Math.abs(dx) > Math.abs(dy) * 1.5) { setRevealed(dx < 0); swiped.current = true; }
        }}
        onTouchCancel={() => { start.current = null; }}
        onClickCapture={(e) => {
          if (swiped.current || revealed) { e.preventDefault(); e.stopPropagation(); if (!swiped.current) setRevealed(false); swiped.current = false; }
        }}
      >{children}</div>
    </div>
  );
}

function SharedView({ token }) {
  const [state, setState] = useState('loading'); // loading | ok | not_found
  const [data, setData] = useState(null);
  const [authenticated, setAuthenticated] = useState(false);

  useEffect(() => {
    api('/share/' + token)
      .then((res) => {
        if (!res.ok) { setState('not_found'); return; }
        return res.json().then((d) => { setData(d); setState('ok'); });
      })
      .catch(() => setState('not_found'));
  }, [token]);

  useEffect(() => {
    if (state !== 'not_found') return;
    let active = true;
    api('/session')
      .then((res) => res.ok ? res.json() : null)
      .then((session) => {
        if (active) setAuthenticated(session?.authenticated === true);
      })
      .catch(() => { if (active) setAuthenticated(false); });
    return () => { active = false; };
  }, [state, token]);

  if (state === 'loading') {
    return <div className="min-h-screen grid place-items-center"><div className="spinner" /></div>;
  }

  if (state === 'not_found') {
    return (
      <div className="min-h-screen grid place-items-center px-6 text-center">
        <div>
          <div className="font-mono text-lg mb-2">brief_ai<span className="text-accent glow">$</span></div>
          <p className="text-muted text-sm">This link is no longer available.</p>
          {authenticated && (
            <a
              href="/"
              className="inline-flex mt-5 px-4 py-2 border border-edge rounded-lg text-sm text-accent hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent transition-colors"
            >
              Back to app
            </a>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen px-4 py-10 flex justify-center">
      <div className="w-full max-w-2xl fade-in">
        <div className="font-mono text-sm mb-6 text-muted">
          brief_ai<span className="text-accent glow">$</span>
        </div>

        <div className="corner-panel bg-panel border border-edge rounded-2xl p-5">
          <div className="flex items-center gap-3 text-xs uppercase tracking-wide text-muted mb-1.5">
            <span>{data.prompt === null ? 'Shared answer' : 'Question'}</span>
            <span className="ml-auto normal-case text-[11px]">{fmtTime(data.created_at)}</span>
          </div>
          {data.prompt !== null && <div className="bg-panel2 border border-edge rounded-xl px-4 py-3 text-sm whitespace-pre-wrap">{data.prompt}</div>}
          {data.images && data.images.length > 0 && (
            <div className="mt-3">
              <AttachmentStrip images={data.images} size={76} base={'/api/share/' + token + '/images/'} />
            </div>
          )}

          <div className="flex items-center gap-3 text-xs uppercase tracking-wide text-muted mt-5 mb-2">
            <span>response</span>
            <span className="ml-auto normal-case text-accent2 text-[11px]">{data.model_label}</span>
          </div>
          <Markdown text={data.answer} />
        </div>
      </div>
    </div>
  );
}

function groupByProvider(models) {
  const groups = [];
  models.forEach((m) => {
    let g = groups.find((x) => x.label === m.provider_label);
    if (!g) { g = { label: m.provider_label, items: [] }; groups.push(g); }
    g.items.push(m);
  });
  return groups;
}

const IMAGE_TOKEN_TILE_PX = 28;

function imageTokenEstimate(width, height) {
  return Math.ceil(width / IMAGE_TOKEN_TILE_PX) * Math.ceil(height / IMAGE_TOKEN_TILE_PX);
}

const THUMB_MAX_SIDE = 512;
const THUMB_QUALITY = 0.8;

// Thumbnails are made here rather than on the server: the backend never has to
// decode untrusted image data, and History pulls ~50KB per attachment instead
// of the full upload. Returns null when there is nothing to gain, in which case
// the original is served in the thumbnail's place.
function makeThumbnail(img) {
  const longest = Math.max(img.naturalWidth, img.naturalHeight);
  if (!longest || longest <= THUMB_MAX_SIDE) return null;
  const scale = THUMB_MAX_SIDE / longest;
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(img.naturalWidth * scale));
  canvas.height = Math.max(1, Math.round(img.naturalHeight * scale));
  try {
    canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
    // toDataURL silently falls back to PNG where WebP isn't supported, so read
    // the type back out of the result instead of assuming it.
    const url = canvas.toDataURL('image/webp', THUMB_QUALITY);
    const match = /^data:([^;,]+);base64,/.exec(url);
    if (!match) return null;
    return { base64: url.slice(url.indexOf(',') + 1), mediaType: match[1] };
  } catch (e) {
    return null;
  }
}

function readImageFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error('read_failed'));
    reader.onload = () => {
      const dataUrl = reader.result;
      const base64 = dataUrl.slice(dataUrl.indexOf(',') + 1);
      const finish = (width, height, thumb) => resolve({
        id: Math.random().toString(36).slice(2),
        base64,
        name: file.name,
        mediaType: file.type,
        sizeBytes: file.size,
        previewUrl: dataUrl,
        width,
        height,
        thumb,
      });
      const img = new Image();
      img.onload = () => finish(img.naturalWidth || 1000, img.naturalHeight || 1000, makeThumbnail(img));
      img.onerror = () => finish(1000, 1000, null); // rough fallback if decoding fails
      img.src = dataUrl;
    };
    reader.readAsDataURL(file);
  });
}

function imageUrl(sha, thumb, base) {
  return (base || '/api/images/') + sha + (thumb ? '/thumb' : '');
}

const ImagePreviewContext = React.createContext(null);

function previewImage(image, base) {
  const extension = { 'image/jpeg': 'jpg', 'image/png': 'png', 'image/gif': 'gif', 'image/webp': 'webp' }[image.mediaType || image.media_type] || 'jpg';
  return {
    src: image.previewUrl || imageUrl(image.sha, false, base),
    name: image.name || `image-${image.sha || image.id}.${extension}`,
  };
}

function ImagePreviewProvider({ children }) {
  const [gallery, setGallery] = useState(null);
  const open = (images, index = 0, base) => {
    if (images.length) setGallery({ images: images.map((im) => previewImage(im, base)), index });
  };
  return (
    <ImagePreviewContext.Provider value={open}>
      {children}
      {gallery && <ImagePreview images={gallery.images} initialIndex={gallery.index} onClose={() => setGallery(null)} />}
    </ImagePreviewContext.Provider>
  );
}

function ImagePreview({ images, initialIndex, onClose }) {
  const [index, setIndex] = useState(initialIndex);
  const [zoom, setZoom] = useState(1);
  const [status, setStatus] = useState('loading');
  const [naturalSize, setNaturalSize] = useState({ width: 1, height: 1 });
  const [viewportSize, setViewportSize] = useState({ width: 1, height: 1 });
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState('');
  const dialogRef = useRef(null);
  const viewportRef = useRef(null);
  const swipeRef = useRef(null);
  const mountedRef = useRef(true);
  const current = images[index];
  const fitScale = Math.min(Math.max(1, viewportSize.width - 32) / naturalSize.width, Math.max(1, viewportSize.height - 32) / naturalSize.height, 1);
  const displayWidth = naturalSize.width * fitScale * zoom;
  const displayHeight = naturalSize.height * fitScale * zoom;
  const move = (delta) => {
    setIndex((i) => (i + delta + images.length) % images.length);
    setZoom(1);
    setStatus('loading');
    setDownloadError('');
    if (viewportRef.current) viewportRef.current.scrollTo(0, 0);
  };

  useEffect(() => {
    mountedRef.current = true;
    const dialog = dialogRef.current;
    const previousFocus = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog.showModal();
    const observer = new ResizeObserver(([entry]) => {
      setViewportSize({ width: entry.contentRect.width, height: entry.contentRect.height });
    });
    observer.observe(viewportRef.current);
    const onKeyDown = (event) => {
      if (images.length > 1 && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
        event.preventDefault();
        move(event.key === 'ArrowLeft' ? -1 : 1);
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      mountedRef.current = false;
      observer.disconnect();
      dialog.close();
      document.body.style.overflow = previousOverflow;
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
    };
  }, []);

  const download = async () => {
    setDownloading(true);
    setDownloadError('');
    try {
      const response = await fetch(current.src, { credentials: 'same-origin' });
      if (!response.ok) throw new Error('download_failed');
      const blob = await response.blob();
      if (!blob.type.startsWith('image/')) throw new Error('not_an_image');
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = current.name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch {
      if (mountedRef.current) setDownloadError('Could not download this image. Please try again.');
    } finally {
      if (mountedRef.current) setDownloading(false);
    }
  };

  return (
    <dialog
      ref={dialogRef}
      className="image-preview"
      aria-label="Image preview"
      onCancel={(e) => { e.preventDefault(); onClose(); }}
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <header className="image-preview-toolbar">
        <div className="image-preview-title">
          <span className="font-mono text-accent text-xs">IMAGE {index + 1} / {images.length}</span>
          <span className="image-preview-name" title={current.name}>{current.name}</span>
        </div>
        <div className="image-preview-actions">
          <button type="button" aria-label="Zoom out" disabled={zoom === 1 || status !== 'ready'} onClick={() => setZoom((z) => Math.max(1, z - 0.5))}>−</button>
          <button type="button" aria-label="Fit image to screen" onClick={() => { setZoom(1); viewportRef.current.scrollTo(0, 0); }}>{zoom === 1 ? 'Fit' : `${Math.round(zoom * 100)}%`}</button>
          <button type="button" aria-label="Zoom in" disabled={zoom === 4 || status !== 'ready'} onClick={() => setZoom((z) => Math.min(4, z + 0.5))}>+</button>
          <button type="button" onClick={download} disabled={downloading || status !== 'ready'}>{downloading ? 'Saving…' : 'Download'}</button>
          <button type="button" aria-label="Close image preview" autoFocus onClick={onClose}>✕</button>
        </div>
      </header>
      <div
        ref={viewportRef}
        className="image-preview-viewport"
        onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
        onTouchStart={(e) => {
          swipeRef.current = zoom === 1 && e.touches.length === 1
            ? { x: e.touches[0].clientX, y: e.touches[0].clientY } : null;
        }}
        onTouchEnd={(e) => {
          const start = swipeRef.current;
          swipeRef.current = null;
          if (!start || images.length < 2) return;
          const dx = e.changedTouches[0].clientX - start.x;
          const dy = e.changedTouches[0].clientY - start.y;
          if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) move(dx < 0 ? 1 : -1);
        }}
        onTouchCancel={() => { swipeRef.current = null; }}
      >
        <div className="image-preview-canvas" style={{ width: Math.max(viewportSize.width, displayWidth), height: Math.max(viewportSize.height, displayHeight) }} onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
          {status === 'loading' && <span className="spinner" role="status" aria-label="Loading image" />}
          {status === 'error' && <p role="alert">This image is no longer available.</p>}
          <img
            key={index}
            src={current.src}
            alt={current.name}
            draggable="false"
            style={{ display: status === 'ready' ? 'block' : 'none', width: displayWidth, height: displayHeight }}
            onLoad={(e) => { setNaturalSize({ width: e.currentTarget.naturalWidth, height: e.currentTarget.naturalHeight }); setStatus('ready'); }}
            onError={() => setStatus('error')}
          />
        </div>
      </div>
      <footer className="image-preview-footer">
        <button type="button" aria-label="Previous image" disabled={images.length < 2} onClick={() => move(-1)}>← Previous</button>
        <span role="status">{downloadError || 'Esc to close · Scroll to explore when zoomed'}</span>
        <button type="button" aria-label="Next image" disabled={images.length < 2} onClick={() => move(1)}>Next →</button>
      </footer>
    </dialog>
  );
}

function AttachmentStrip({ images, size, base }) {
  const openPreview = React.useContext(ImagePreviewContext);
  if (!images || !images.length) return null;
  const px = size || 56;
  return (
    <div className="flex flex-wrap gap-2">
      {images.map((im, index) => (
        <button type="button" key={im.sha + '-' + index} className="image-preview-trigger" aria-label={`Preview image ${index + 1}`} onClick={(e) => { e.stopPropagation(); openPreview(images, index, base); }}>
          <img
            src={imageUrl(im.sha, im.has_thumb, base)}
            alt=""
            loading="lazy"
            style={{ width: px, height: px }}
            className="border border-edge bg-panel2 object-cover shrink-0"
          />
        </button>
      ))}
    </div>
  );
}

function ImageThumb({ image, images, index, onRemove }) {
  const openPreview = React.useContext(ImagePreviewContext);
  return (
    <div className="flex flex-col items-center gap-1">
      <div className="relative w-[52px] h-[52px] border border-edge bg-panel2">
        <button type="button" className="image-preview-trigger w-full h-full" aria-label={`Preview image ${index + 1}`} onClick={() => openPreview(images, index)}>
          <img src={image.previewUrl} alt="" className="w-full h-full object-cover" />
        </button>
        <button
          type="button"
          onClick={() => onRemove(image.id)}
          title="Remove"
          aria-label={`Remove image ${index + 1}`}
          className="absolute -top-1 -right-1 w-4 h-4 bg-ink border border-edge flex items-center justify-center text-muted hover:text-white"
        >
          <svg width="8" height="8" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.6"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </div>
      <span className="font-mono text-[9px] text-muted/70">{(image.sizeBytes / 1e6).toFixed(1)}mb</span>
    </div>
  );
}

const DEFAULT_IMAGE_LIMITS = {
  max_images: 20,
  max_image_mb: 10,
  max_total_mb: 24,
  allowed_types: ['image/jpeg', 'image/png', 'image/gif', 'image/webp'],
};

function TrashIcon() {
  return <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6" /><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" /></svg>;
}

function AskView({ models, model, setModel, rate, imageLimits, settings, onUnauth, conversationId, onConversationChange }) {
  const limits = imageLimits || DEFAULT_IMAGE_LIMITS;
  const groups = useMemo(() => groupByProvider(models), [models]);
  const currentModel = models.find((m) => m.id === model) || null;
  const [prompt, setPrompt] = useState('');
  const [images, setImages] = useState([]);
  const [imageError, setImageError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [shareMessage, setShareMessage] = useState(null);
  const [conversation, setConversation] = useState(null);
  const [restoring, setRestoring] = useState(!!conversationId);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [deleting, setDeleting] = useState(false);
  const [deleteMessage, setDeleteMessage] = useState(null);
  const [copied, setCopied] = useState(null);
  const fileInputRef = useRef(null);
  const requestRef = useRef(null);
  const unauthRef = useRef(onUnauth);
  unauthRef.current = onUnauth;
  const busy = ['queued', 'running'].includes(conversation?.job?.status);
  const loading = submitting || busy;
  const unanswered = conversation?.messages?.at(-1)?.role === 'user';

  useEffect(() => {
    let cancelled = false;
    let timer;
    setConversation(null);
    setRestoring(!!conversationId);
    setPrompt(''); setImages([]); setImageError('');
    requestRef.current = null;
    setError('');
    let firstPoll = true;
    const poll = async () => {
      try {
        const res = await api('/conversations/' + conversationId);
        if (cancelled) return;
        if (res.status === 401) { unauthRef.current(); return; }
        if (res.status === 404) { onConversationChange(null); return; }
        if (!res.ok) throw new Error('Could not load this conversation.');
        const data = await res.json();
        if (cancelled) return;
        setConversation(data);
        if (firstPoll) {
          const lastModel = data.messages.filter((m) => m.role === 'user').at(-1)?.model;
          if (models.some((m) => m.id === lastModel)) setModel(lastModel);
          firstPoll = false;
        }
        setRestoring(false);
        setError('');
        if (['queued', 'running'].includes(data.job?.status)) timer = setTimeout(poll, 1200);
      } catch (e) {
        if (!cancelled) { setError('Connection lost. Reconnecting…'); timer = setTimeout(poll, 3000); }
      }
    };
    if (conversationId) poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [conversationId, refresh, onConversationChange]);

  useEffect(() => {
    if (conversationId) return;
    let pending;
    try { pending = sessionStorage.getItem('briefai_pending_request'); } catch {}
    if (!pending) return;
    let cancelled = false;
    setRestoring(true);
    api('/conversation-requests/' + pending).then(async (res) => {
      if (cancelled) return;
      if (res.status === 401) { unauthRef.current(); return; }
      if (res.ok) {
        const data = await res.json();
        if (!cancelled) { onConversationChange(data.id); sessionStorage.removeItem('briefai_pending_request'); }
      } else if (res.status !== 404) throw new Error('Could not recover the last request.');
    }).catch(() => { if (!cancelled) setError('Could not recover the last request. Refresh to try again.'); })
      .finally(() => { if (!cancelled) setRestoring(false); });
    return () => { cancelled = true; };
  }, [conversationId, onConversationChange]);

  const newConversation = () => {
    if (submitting) return;
    requestRef.current = null;
    try { sessionStorage.removeItem('briefai_pending_request'); } catch {}
    setPrompt(''); setImages([]); setImageError(''); setError(''); setConversation(null);
    onConversationChange(null);
  };

  const retry = async () => {
    setSubmitting(true); setError('');
    try {
      const res = await api('/conversations/' + conversationId + '/retry', { method: 'POST' });
      if (res.status === 401) { unauthRef.current(); return; }
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || data.error || 'Could not retry.');
      setRefresh((x) => x + 1);
    } catch (e) { setError(e.message); }
    finally { setSubmitting(false); }
  };

  const truncate = async () => {
    setDeleting(true); setError('');
    try {
      const res = await api('/conversations/' + conversationId + '/messages/' + deleteMessage, { method: 'DELETE' });
      if (res.status === 401) { unauthRef.current(); return; }
      const data = await res.json();
      if (!res.ok) throw new Error('Could not delete the message.');
      setDeleteMessage(null);
      if (data.deleted_conversation) newConversation();
      else setRefresh((x) => x + 1);
    } catch (e) { setError(e.message); }
    finally { setDeleting(false); }
  };

  const totalImageBytes = useMemo(() => images.reduce((s, im) => s + im.sizeBytes, 0), [images]);
  const imagesTokenTotal = useMemo(
    () => images.reduce((s, im) => s + imageTokenEstimate(im.width, im.height), 0),
    [images],
  );
  const unsupportedImages = currentModel?.supports_images === false && (images.length > 0 || conversation?.messages?.some((m) => m.images?.length));
  const overLimit = images.length > limits.max_images || totalImageBytes > limits.max_total_mb * 1e6;

  const addFiles = async (fileList) => {
    if (currentModel?.supports_images === false) {
      setImageError("Local AI supports text only. Paste your code into the prompt.");
      return;
    }
    const files = Array.from(fileList || []).filter((f) => limits.allowed_types.includes(f.type));
    if (!files.length) return;
    let entries;
    try {
      entries = await Promise.all(files.map(readImageFile));
    } catch (e) {
      setImageError('Could not read one of the files.');
      return;
    }
    const oversized = entries.find((e) => e.sizeBytes > limits.max_image_mb * 1e6);
    if (oversized) {
      setImageError(`One image is ${(oversized.sizeBytes / 1e6).toFixed(1)}MB, max is ${limits.max_image_mb}MB per image.`);
      return;
    }
    setImages((prev) => {
      const next = [...prev, ...entries];
      const nextBytes = next.reduce((s, im) => s + im.sizeBytes, 0);
      if (next.length > limits.max_images || nextBytes > limits.max_total_mb * 1e6) {
        setImageError(
          `img ${next.length}/${limits.max_images} · ${(nextBytes / 1e6).toFixed(1)}/${limits.max_total_mb}mb `
          + '- remove an image to continue.'
        );
        return prev;
      }
      setImageError('');
      return next;
    });
  };

  const removeImage = (id) => {
    setImageError('');
    setImages((prev) => prev.filter((i) => i.id !== id));
  };

  const onDrop = (e) => {
    e.preventDefault();
    if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
  };
  const onDragOver = (e) => e.preventDefault();

  const onTextareaPaste = (e) => {
    const items = Array.from(e.clipboardData ? e.clipboardData.items : []);
    const files = items.filter((it) => it.type.startsWith('image/')).map((it) => it.getAsFile()).filter(Boolean);
    if (files.length) addFiles(files);
  };

  const estimate = useMemo(() => {
    if (!currentModel) return null;
    const historyChars = (conversation?.messages || []).reduce((total, m) => total + m.content.length, 0);
    const chars = historyChars + (conversation ? (conversation.context || '').length + prompt.length + 12 : estimateChars(prompt, settings, true));
    if (!prompt.trim() && images.length === 0) return null;
    const historyImages = (conversation?.messages || []).flatMap((m) => m.images || []).reduce((total, im) => total + imageTokenEstimate(im.width, im.height), 0);
    const inTok = Math.ceil(chars / 4) + imagesTokenTotal + historyImages;
    return (inTok / 1e6 * currentModel.input) * rate;
  }, [prompt, currentModel, settings, imagesTokenTotal, rate, images.length, conversation]);

  const send = async () => {
    if (!prompt.trim() || loading || restoring || unanswered || overLimit || unsupportedImages) return;
    setSubmitting(true); setError('');
    const body = {
      model, prompt: prompt.trim(), brief: true,
      ...(!conversationId ? { context: buildContext(settings) } : {}),
      images: images.map((im) => ({
        data: im.base64, media_type: im.mediaType, width: im.width, height: im.height,
        ...(im.thumb ? { thumb: im.thumb.base64, thumb_media_type: im.thumb.mediaType } : {}),
      })),
    };
    // Preserve the submission ID across network retries; resolve it before allowing an edited retry.
    if (!requestRef.current) requestRef.current = { id: crypto.randomUUID(), body };
    const submission = requestRef.current;
    try { sessionStorage.setItem('briefai_pending_request', submission.id); } catch {}
    try {
      const res = await api(conversationId ? '/conversations/' + conversationId + '/messages' : '/conversations', {
        method: 'POST', body: JSON.stringify({ ...submission.body, request_id: submission.id }),
      });
      if (res.status === 401) { onUnauth(); return; }
      const data = await res.json();
      if (!res.ok) {
        requestRef.current = null;
        try { sessionStorage.removeItem('briefai_pending_request'); } catch {}
        throw new Error(data.detail || data.error || 'Request failed.');
      }
      onConversationChange(data.id);
      setRefresh((x) => x + 1);
      setPrompt(''); setImages([]); setImageError('');
      requestRef.current = null;
      try { sessionStorage.removeItem('briefai_pending_request'); } catch {}
    } catch (e) { setError(e.message || 'Network error. Retry to check the same submission.'); }
    finally { setSubmitting(false); }
  };

  const clearAll = () => {
    setPrompt('');
    setImages([]);
    setImageError('');
    setError('');
  };

  const onKeyDown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      send();
    }
  };

  const sendDisabled = loading || restoring || unanswered || !prompt.trim() || overLimit || unsupportedImages;

  return (
    <div className="space-y-4">
      {shareMessage && <ShareDialog itemId={shareMessage} basePath="/conversation-messages/" onClose={() => setShareMessage(null)} onUnauth={onUnauth} />}
      {conversationId && <div className="flex justify-end"><button type="button" className="font-mono text-xs text-muted hover:text-accent" onClick={newConversation} disabled={submitting}>+ New conversation</button></div>}
      {restoring && <p className="text-sm text-muted" role="status">Loading conversation…</p>}
      {conversation?.messages.map((message) => (
        <div key={message.id} className={'corner-panel border border-edge ' + (message.role === 'user' ? 'bg-panel2' : 'bg-panel')}>
          <div className="flex items-center gap-3 px-4 py-2 border-b border-edge font-mono text-[11px] text-muted">
            <span>{message.role === 'user' ? 'You' : models.find((m) => m.id === message.model)?.label || message.model}</span>
            <time dateTime={message.created_at}>{fmtTime(message.created_at)}</time>
            {message.role === 'user'
              ? <button type="button" className="ml-auto hover:text-bad p-1" aria-label="Delete message and later replies" title="Delete message and later replies" disabled={deleting} onClick={() => setDeleteMessage(message.id)}><TrashIcon /></button>
              : <button type="button" className="ml-auto hover:text-white" onClick={async () => { try { await navigator.clipboard.writeText(message.content); setCopied(message.id); setTimeout(() => setCopied(null), 1500); } catch { setError('Clipboard is unavailable. Select the answer to copy it.'); } }}>{copied === message.id ? 'copied' : 'copy'}</button>}
            {message.role === 'assistant' && <button type="button" onClick={() => setShareMessage(message.id)} className="hover:text-accent2">Share</button>}
          </div>
          <div className="p-5">
            {message.role === 'user' ? <div className="whitespace-pre-wrap break-words">{message.content}</div> : <><Meta result={{ ...message, model_label: models.find((m) => m.id === message.model)?.label || message.model }} rate={rate} /><Markdown text={message.content} /></>}
            {!!message.images?.length && <div className="mt-3"><AttachmentStrip images={message.images} size={64} /></div>}
          </div>
        </div>
      ))}
      {conversation?.job?.status === 'failed' && <div role="alert" className="border border-bad/40 p-4 text-sm"><p>{conversation.job.error_detail}</p><button type="button" className="deck-button mt-3" onClick={retry} disabled={submitting}>Retry answer</button></div>}
      {deleteMessage && <Modal title="Delete from this message?" onClose={() => { if (!deleting) setDeleteMessage(null); }}><p className="text-sm text-muted">This message and all later replies will be deleted.</p><div className="flex gap-3 mt-6"><button type="button" className="deck-button" disabled={deleting} onClick={() => setDeleteMessage(null)}>Keep messages</button><button type="button" className="deck-button danger" disabled={deleting} onClick={truncate}>{deleting ? 'Deleting…' : 'Delete'}</button></div></Modal>}
      <div
        className="corner-panel border border-edge bg-panel focus-within:border-accent/60 transition-colors"
        onDrop={onDrop}
        onDragOver={onDragOver}
      >
        <div className="flex items-center justify-between px-4 py-2 border-b border-edge">
          <span className="font-mono text-[10px] tracking-widest text-muted/80 uppercase">prompt</span>
        </div>

        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onTextareaPaste}
          placeholder={conversationId ? "Follow up…" : "Ask anything…"}
          rows={3}
          className="w-full bg-transparent resize-y outline-none px-4 pt-3 pb-1 text-[0.95rem] placeholder:text-muted/70 min-h-[4.5rem]"
        />

        {images.length > 0 && (
          <div className="flex flex-wrap gap-2.5 px-4 pt-3 pb-1">
            {images.map((im, index) => (
              <ImageThumb key={im.id} image={im} images={images} index={index} onRemove={removeImage} />
            ))}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 border-t border-edge font-mono text-[11px] text-muted">
          <select
            value={model}
            onChange={(e) => setModel(e.target.value)}
            className="bg-panel2 border border-edge rounded text-[11px] font-mono px-2 py-1.5 outline-none hover:border-accent/50 transition cursor-pointer text-[#e8e2d6]"
          >
            {groups.map((g) => (
              <optgroup key={g.label} label={g.label}>
                {g.items.map((m) => (
                  <option key={m.id} value={m.id}>{m.label}</option>
                ))}
              </optgroup>
            ))}
          </select>


          <button
            type="button"
            onClick={() => fileInputRef.current && fileInputRef.current.click()}
            disabled={currentModel?.supports_images === false}
            title={currentModel?.supports_images === false ? "This model supports text only" : "Attach images"}
            className="flex items-center gap-1.5 text-muted hover:text-white transition-colors"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M21.44 11.05l-9.19 9.19a5 5 0 01-7.07-7.07l8.49-8.49a3.5 3.5 0 014.95 4.95l-8.49 8.49a2 2 0 01-2.83-2.83l7.78-7.78" />
            </svg>
            <span className={overLimit ? 'text-bad' : images.length ? 'text-good' : ''}>
              {images.length}/{limits.max_images} · {(totalImageBytes / 1e6).toFixed(1)}/{limits.max_total_mb}mb
            </span>
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept={limits.allowed_types.join(',')}
            className="hidden"
            onChange={(e) => { addFiles(e.target.files); e.target.value = ''; }}
          />

          <div className="ml-auto flex items-center gap-3">
            {estimate != null && (
              <span className="text-muted/70 whitespace-nowrap">est_in: {zl(estimate)}</span>
            )}
            <button
              onClick={clearAll}
              disabled={!prompt && !error && images.length === 0}
              title="Clear"
              className="text-muted hover:text-white disabled:opacity-40 p-1 transition-colors"
            >
              <TrashIcon />
            </button>

            <button
              type="button"
              onClick={send}
              disabled={sendDisabled}
              className="flex items-center gap-2 bg-accent hover:bg-accent2 disabled:opacity-40 disabled:hover:bg-accent text-accentInk text-[11px] font-bold px-4 py-1.5 transition-colors"
            >
              {loading ? <span className="spinner" /> : null}
              {loading ? 'thinking…' : conversationId ? 'send' : 'brief'}
            </button>
          </div>
        </div>
      </div>

      {conversationId && <p className="font-mono text-[10px] text-muted">Saved automatically</p>}
      {unsupportedImages && <p role="alert" className="text-sm text-bad">This conversation contains images. Select a model that supports images.</p>}
      {currentModel?.provider === "local" && (
        <p className="font-mono text-xs text-muted" role="status">
          Local AI · 8,192-token context · up to 1,024 output tokens · text only · may take up to 4 minutes.
          {unsupportedImages ? " Remove attached images to continue." : " Paste code with filenames to include several files."}
        </p>
      )}

      {imageError && (
        <div className="border border-bad/40 bg-bad/10 text-bad font-mono text-[11px] px-3.5 py-2.5">
          ! {imageError}
        </div>
      )}

      {loading && (
        <div className="text-sm text-muted flex items-center gap-2 px-1">
          <span className="spinner" />
          <span>{conversation?.job?.status === 'queued' ? 'Queued…' : 'Generating…'}</span>
        </div>
      )}

      {error && (
        <div className="bg-red-950/40 border border-bad/40 text-red-300 text-sm rounded-xl px-4 py-3">
          {error}
        </div>
      )}


    </div>
  );
}

const HISTORY_PAGE = 30;

function startOfWeek(d) {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
  return x;
}

function dayKey(d) {
  return d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate();
}

function weekKey(d) {
  const s = startOfWeek(d);
  return 'w' + s.getFullYear() + '-' + (s.getMonth() + 1) + '-' + s.getDate();
}

function dayLabel(d) {
  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(today.getDate() - 1);
  const dstr = d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
  if (dayKey(d) === dayKey(today)) return 'Today (' + dstr + ')';
  if (dayKey(d) === dayKey(yesterday)) return 'Yesterday (' + dstr + ')';
  return d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
}

function weekLabel(d) {
  if (weekKey(d) === weekKey(new Date())) return 'This week';
  const s = startOfWeek(d);
  const e = new Date(s);
  e.setDate(s.getDate() + 6);
  return s.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })
    + ' - ' + e.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
}

function groupHistory(items) {
  const weeks = [];
  const wmap = {};
  items.forEach((it) => {
    const d = new Date(it.created_at);
    const wk = weekKey(d);
    let w = wmap[wk];
    if (!w) { w = { key: wk, label: weekLabel(d), days: [], dmap: {} }; wmap[wk] = w; weeks.push(w); }
    const dk = dayKey(d);
    let day = w.dmap[dk];
    if (!day) { day = { key: dk, label: dayLabel(d), items: [] }; w.dmap[dk] = day; w.days.push(day); }
    day.items.push(it);
  });
  return weeks;
}

function ConversationSharedAnswers({ onUnauth, onOpen }) {
  const [items, setItems] = useState([]);
  const [sharing, setSharing] = useState(null);
  const [more, setMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const load = async (before) => {
    setBusy(true); setError('');
    try {
      const res = await api('/conversation-shares' + (before ? '?before=' + before : ''));
      if (res.status === 401) { onUnauth(); return; }
      if (!res.ok) throw new Error('Could not load shared conversation answers.');
      const data = await res.json();
      setItems((prev) => before ? [...prev, ...data.items] : data.items);
      setMore(data.items.length === 30);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  useEffect(() => { load(); }, []);
  return <section className="mb-6 space-y-3">
    {!!items.length && <h2 className="font-mono text-xs uppercase tracking-widest text-accent2">Shared conversation answers</h2>}
    {items.map((item) => <div className="border-b border-edge py-3 flex gap-3 items-center" key={item.id}><button className="text-left flex-1 text-sm" onClick={() => onOpen(item.conversation_id)}>{item.prompt_preview}</button><a className="text-xs text-accent2" href={'/s/' + item.share_token} target="_blank" rel="noopener noreferrer">Open ↗</a><button className="text-xs text-muted" onClick={() => setSharing(item.id)}>Manage sharing</button></div>)}
    {error && <p role="alert" className="text-sm text-bad">{error} <button className="underline" onClick={() => load()}>Retry</button></p>}
    {more && <button className="deck-button" disabled={busy} onClick={() => load(items.at(-1)?.id)}>Load more</button>}
    {sharing && <ShareDialog itemId={sharing} basePath="/conversation-messages/" onUnauth={onUnauth} onClose={() => setSharing(null)} onChanged={() => load()} />}
  </section>;
}

function ConversationHistory({ onOpen, onUnauth }) {
  const [items, setItems] = useState([]);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [hasMore, setHasMore] = useState(false);
  const unauthRef = useRef(onUnauth);
  unauthRef.current = onUnauth;
  const generation = useRef(0);
  const load = async (before, version = generation.current) => {
    setLoading(true); setError('');
    const params = new URLSearchParams({ q: query });
    if (before) params.set('before', before);
    try {
      const res = await api('/conversations?' + params);
      if (res.status === 401) { unauthRef.current(); return; }
      if (!res.ok) throw new Error('Could not load conversations.');
      const data = await res.json();
      if (version !== generation.current) return;
      setItems((prev) => before ? [...prev, ...data.items] : data.items);
      setHasMore(data.items.length === 30);
    } catch (e) { if (version === generation.current) setError(e.message); }
    finally { if (version === generation.current) setLoading(false); }
  };
  useEffect(() => {
    const version = ++generation.current;
    const timer = setTimeout(() => load(null, version), 200);
    return () => { clearTimeout(timer); generation.current++; };
  }, [query]);
  return <section className="mb-8 space-y-3">
    <h1 className="font-mono text-xs uppercase tracking-widest text-accent2">Conversations</h1>
    <input aria-label="Search conversations" className="deck-input w-full" placeholder="Search conversations" value={query} onChange={(e) => setQuery(e.target.value)} />
    {items.map((item) => <button type="button" key={item.id} onClick={() => onOpen(item.id)} className="block w-full text-left border-b border-edge py-3 hover:text-accent2">
      <span className="block text-sm break-words">{item.title || item.prompt_preview}</span>
      <span className="block font-mono text-[11px] text-muted mt-1">{fmtTime(item.created_at)} · {item.message_count} messages{['queued', 'running'].includes(item.status) ? ' · generating…' : item.status === 'failed' ? ' · failed' : ''}</span>
    </button>)}
    {!loading && !items.length && <p className="text-sm text-muted">{query ? 'No matching conversations.' : 'No conversations yet.'}</p>}
    {error && <p role="alert" className="text-sm text-bad">{error} <button className="underline" onClick={() => load(null)}>Retry</button></p>}
    {loading && <p role="status" className="text-sm text-muted">Loading…</p>}
    {hasMore && <button type="button" className="deck-button" disabled={loading} onClick={() => load(items.at(-1)?.id)}>Load more</button>}
    <h2 className="font-mono text-xs uppercase tracking-widest text-muted pt-5">Earlier answers</h2>
  </section>;
}

function HistoryView({ rate, onUnauth, sharedOnly = false }) {
  const [items, setItems] = useState([]);
  const [q, setQ] = useState('');
  const [loading, setLoading] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [openState, setOpenState] = useState({});
  const [detail, setDetail] = useState(null);
  const [shareItem, setShareItem] = useState(null);
  const [deleteItem, setDeleteItem] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');

  const itemsRef = useRef([]);
  const loadingRef = useRef(false);
  const sentinelRef = useRef(null);
  useEffect(() => { itemsRef.current = items; }, [items]);

  const curWeek = weekKey(new Date());
  const curDay = dayKey(new Date());

  const fetchPage = useCallback(async (reset) => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    setLoading(true);
    const params = new URLSearchParams();
    if (sharedOnly) params.set('shared', 'true');
    if (q.trim()) params.set('q', q.trim());
    if (!reset && itemsRef.current.length) {
      params.set('before', itemsRef.current[itemsRef.current.length - 1].id);
    }
    try {
      const res = await api('/history?' + params.toString());
      if (res.status === 401) { onUnauth(); return; }
      const data = await res.json();
      const batch = data.items || [];
      setItems((prev) => (reset ? batch : [...prev, ...batch]));
      setHasMore(batch.length >= HISTORY_PAGE);
    } catch (e) {
      setHasMore(false);
    } finally {
      loadingRef.current = false;
      setLoading(false);
      setLoaded(true);
    }
  }, [q, onUnauth, sharedOnly]);

  useEffect(() => {
    const t = setTimeout(() => {
      setItems([]);
      itemsRef.current = [];
      setHasMore(true);
      setLoaded(false);
      fetchPage(true);
    }, 300);
    return () => clearTimeout(t);
  }, [q, fetchPage]);

  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const ob = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting && hasMore && !loadingRef.current && itemsRef.current.length > 0) {
        fetchPage(false);
      }
    }, { rootMargin: '250px' });
    ob.observe(el);
    return () => ob.disconnect();
  }, [hasMore, fetchPage]);

  const toggleShare = (it, e) => { e.stopPropagation(); setShareItem(it.id); };
  const shareChanged = (id, token) => {
    setItems((prev) => sharedOnly && !token ? prev.filter((it) => it.id !== id) : prev.map((it) => it.id === id ? { ...it, share_token: token } : it));
    setDetail((prev) => prev && prev.id === id ? { ...prev, share_token: token } : prev);
    if (sharedOnly && token && !itemsRef.current.some((it) => it.id === id)) fetchPage(true);
  };

  const isOpen = (key, type) => {
    if (openState[key] !== undefined) return openState[key];
    return sharedOnly || (type === 'week' ? key === curWeek : key === curDay);
  };
  const toggle = (key, type) => setOpenState((s) => ({ ...s, [key]: !isOpen(key, type) }));

  const open = async (id) => {
    try {
      const res = await api('/history/' + id);
      if (res.status === 401) { onUnauth(); return; }
      if (res.ok) setDetail(await res.json());
    } catch (e) { /* noop */ }
  };

  const del = (id, e) => { if (e) e.stopPropagation(); setDeleteError(''); setDeleteItem(id); };
  const confirmDelete = async () => {
    setDeleting(true); setDeleteError('');
    try {
      const res = await api('/history/' + deleteItem, { method: 'DELETE' });
      if (res.status === 401) { onUnauth(); return; }
      if (!res.ok) throw new Error('Could not delete this answer. Please try again.');
      setItems((prev) => prev.filter((x) => x.id !== deleteItem));
      if (detail && detail.id === deleteItem) setDetail(null);
      setDeleteItem(null);
    } catch (e) { setDeleteError(e.message); }
    finally { setDeleting(false); }
  };

  const groups = useMemo(() => groupHistory(items), [items]);

  return (
    <div className="space-y-3">
      {sharedOnly && <div className="corner-panel bg-panel border border-edge px-4 py-3 mb-5">
        <h1 className="font-mono text-xs uppercase tracking-widest text-accent2">Shared answers</h1>
        <p className="text-sm text-muted mt-2">Your active public links. Open an answer or choose whether its question and images are included.</p>
      </div>}
      <div className="relative">
        <svg
          className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-muted pointer-events-none"
          viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
          strokeLinecap="round" strokeLinejoin="round"
        >
          <circle cx="11" cy="11" r="7" />
          <line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search"
          className="w-full bg-panel border border-edge text-sm pl-10 pr-4 py-2.5 outline-none focus:border-accent/60 transition-colors"
        />
      </div>

      {loaded && items.length === 0 && (
        <div className="text-muted text-sm px-1">{q.trim() ? 'No matches.' : sharedOnly ? 'No shared answers yet. Publish an answer from Ask or History to see it here.' : 'No prompts yet.'}</div>
      )}

      {groups.map((w) => {
        const wOpen = isOpen(w.key, 'week');
        return (
          <div key={w.key} className="space-y-2">
            <button
              onClick={() => toggle(w.key, 'week')}
              className="w-full flex items-center gap-2 text-left text-sm font-semibold text-white/90 py-1"
            >
              <span className="text-muted text-xs w-3">{wOpen ? '▾' : '▸'}</span>
              {w.label}
            </button>

            {wOpen && w.days.map((day) => {
              const dOpen = isOpen(day.key, 'day');
              return (
                <div key={day.key} className="pl-4 space-y-1.5">
                  <button
                    onClick={() => toggle(day.key, 'day')}
                    className="w-full flex items-center gap-2 text-left text-xs font-medium text-muted py-0.5"
                  >
                    <span className="w-3">{dOpen ? '▾' : '▸'}</span>
                    {day.label}
                    <span className="text-muted/60">· {day.items.length}</span>
                  </button>

                  {dOpen && day.items.map((it) => (
                    <SwipeRow key={it.id} onDelete={() => del(it.id)}>
                    <div
                      key={it.id}
                      className="group flex items-start gap-2 bg-panel border border-edge hover:border-accent/50 px-4 py-3 transition-colors"
                    >
                      <div className="flex-1 min-w-0">
                      <button onClick={() => open(it.id)} className="w-full text-left">
                        <div className="history-meta flex items-center gap-3 text-xs text-muted mb-1.5">
                          <span className="text-accent2 font-medium">{it.model_label}</span>
                          <span>{fmtTime(it.created_at)}</span>
                          <span className="ml-auto">{zl(it.cost_usd * rate)}</span>
                          <span>{fmtDuration(it.duration_ms)}</span>
                        </div>
                        <div className="text-sm text-[#dcdce2] line-clamp-2">{it.prompt_preview}</div>
                      </button>
                        {it.images && it.images.length > 0 && (
                          <div className="mt-2">
                            <AttachmentStrip images={it.images} size={22} />
                          </div>
                        )}
                      </div>
                      <div className="shrink-0 flex items-center gap-2.5 opacity-100 transition-opacity">
                        {sharedOnly && it.share_token && <a href={'/s/' + it.share_token} target="_blank" rel="noopener noreferrer" className="text-accent2 text-xs" aria-label="Open public answer">Open ↗</a>}
                        <button
                          onClick={(e) => toggleShare(it, e)}
                          title={it.share_token ? 'Manage sharing' : 'Share'}
                          className="font-mono text-[10px] text-muted hover:text-accent2"
                        >
                          {it.share_token ? 'Shared' : 'Share'}
                        </button>
                        <button
                          onClick={(e) => del(it.id, e)}
                          title="Delete"
                          className="text-muted hover:text-red-400 text-sm"
                        >
                          ✕
                        </button>
                      </div>
                    </div>
                    </SwipeRow>
                  ))}
                </div>
              );
            })}
          </div>
        );
      })}

      {loading && <div className="flex justify-center py-3"><span className="spinner" /></div>}
      <div ref={sentinelRef} className="h-1" />

      {shareItem !== null && <ShareDialog itemId={shareItem} onClose={() => setShareItem(null)} onChanged={shareChanged} onUnauth={onUnauth} />}
      {deleteItem !== null && <Modal title="Delete answer?" onClose={() => { if (!deleting) setDeleteItem(null); }}>
        <p className="text-muted text-sm">This removes the answer and its share link. This cannot be undone.</p>
        {deleteError && <p role="alert" className="text-red-400 mt-4">{deleteError}</p>}
        <div className="flex gap-3 mt-6">
          <button className="deck-button" disabled={deleting} onClick={() => setDeleteItem(null)}>Keep answer</button>
          <button className="deck-button danger" disabled={deleting} onClick={confirmDelete}>{deleting ? 'Deleting…' : 'Delete permanently'}</button>
        </div>
      </Modal>}

      {detail && (
        <div
          className="fixed inset-0 z-20 bg-black/70 backdrop-blur-sm grid place-items-center p-4"
          onClick={() => setDetail(null)}
        >
          <div
            className="bg-panel border border-edge w-full max-w-2xl max-h-[85vh] overflow-y-auto p-5 fade-in"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-4">
              <span className="text-xs text-muted">{fmtTime(detail.created_at)}</span>
              <div className="flex items-center gap-4">
                <button onClick={(e) => toggleShare(detail, e)} className="text-muted hover:text-accent2 text-sm">{detail.share_token ? 'Manage sharing' : 'Share'}</button>
                <button
                  onClick={(e) => del(detail.id, e)}
                  className="text-muted hover:text-red-400 text-sm"
                >
                  Delete
                </button>
                <button
                  onClick={() => setDetail(null)}
                  className="text-muted hover:text-white text-sm"
                >
                  Close
                </button>
              </div>
            </div>
            <div className="flex items-center gap-2 text-xs uppercase tracking-wide text-muted mb-1.5">
              <span>Prompt</span>
              {detail.brief && <span className="font-mono text-[10px] normal-case text-accent2">brief</span>}
            </div>
            <div className="bg-panel2 border border-edge px-4 py-3 text-sm whitespace-pre-wrap">
              {detail.prompt}
            </div>
            {detail.images && detail.images.length > 0 && (
              <div className="mt-3">
                <AttachmentStrip images={detail.images} size={76} />
              </div>
            )}
            {/* Everything the Settings toggles prepended, kept out of the prompt
                itself so it never travels with a shared answer. */}
            {detail.context && (
              <div className="mt-2 mb-5">
                <div className="text-xs uppercase tracking-wide text-muted/70 mb-1.5">Context sent with it</div>
                <div className="bg-panel2/50 border border-edge/60 px-4 py-3 font-mono text-[11px] text-muted whitespace-pre-wrap">
                  {detail.context}
                </div>
              </div>
            )}
            {!detail.context && <div className="mb-5" />}
            <div className="text-xs uppercase tracking-wide text-muted mb-2">Answer</div>
            <Meta result={detail} rate={rate} />
            <Markdown text={detail.answer} />
          </div>
        </div>
      )}
    </div>
  );
}

function ContextControls({ settings, setSettings, onEdit }) {
  return (
    <fieldset className="border border-edge bg-panel px-4 py-3 mb-4">
      <legend className="sr-only">Prompt context</legend>
      <div className="flex items-center justify-between gap-3 mb-3">
        <span className="font-mono text-[10px] uppercase tracking-widest text-muted">Context</span>
        <button type="button" onClick={onEdit} className="font-mono text-[11px] text-accent2 hover:text-white">Edit settings →</button>
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-3">
        {[['nowOn', 'Date & time'], ['locOn', 'Location'], ['userOn', 'Personal context']].map(([key, label]) => (
          <label key={key} className="flex items-center gap-2 text-xs cursor-pointer">
            <input type="checkbox" className="settings-toggle" checked={settings[key]} onChange={(e) => setSettings((prev) => ({ ...prev, [key]: e.target.checked }))} />
            <span>{label}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

function SettingsView({ settings, setSettings, onBack }) {
  const upd = (patch) => setSettings((s) => ({ ...s, ...patch }));
  return (
    <div className="space-y-5 fade-in max-w-2xl">
      <button type="button" onClick={onBack} className="font-mono text-xs text-accent2 hover:text-white">← Back to prompt</button>
      <div className="flex items-center justify-between font-mono text-[10px] uppercase tracking-widest">
        <h1 className="text-accent2">Prompt context</h1>
        <span className="text-good">Auto-saved locally</span>
      </div>
      <p className="text-sm text-muted">Choose the details included with your questions.</p>
      <section className="settings-section corner-panel">
        <label className="flex items-center justify-between gap-4 cursor-pointer">
          <span className="font-mono text-xs uppercase tracking-wider">01 / Date &amp; time</span>
          <input type="checkbox" className="settings-toggle" checked={settings.nowOn} onChange={(e) => upd({ nowOn: e.target.checked })} />
        </label>
        <p className="mt-3">Use the current date and time from your device.</p>
      </section>
      <section className="settings-section corner-panel">
        <label className="flex items-center justify-between gap-4 cursor-pointer">
          <span className="font-mono text-xs uppercase tracking-wider">02 / Location</span>
          <input type="checkbox" className="settings-toggle" checked={settings.locOn} onChange={(e) => upd({ locOn: e.target.checked })} />
        </label>
        <p className="mt-3 mb-3">Add a city or region for more relevant answers.</p>
        <input aria-label="Location context" type="text" value={settings.locText} onChange={(e) => upd({ locText: e.target.value })} disabled={!settings.locOn} placeholder="e.g. Bielsko-Biała" className="deck-input w-full disabled:opacity-40" />
      </section>
      <section className="settings-section corner-panel">
        <label className="flex items-center justify-between gap-4 cursor-pointer">
          <span className="font-mono text-xs uppercase tracking-wider">03 / Personal context</span>
          <input type="checkbox" className="settings-toggle" checked={settings.userOn} onChange={(e) => upd({ userOn: e.target.checked })} />
        </label>
        <p className="mt-3 mb-3">Preferences and background you want the model to consider.</p>
        <textarea aria-label="Personal context" value={settings.userText} onChange={(e) => upd({ userText: e.target.value })} disabled={!settings.userOn} rows={4} placeholder="e.g. preferred language, interests, or dietary preferences" className="deck-input w-full resize-y disabled:opacity-40" />
      </section>
      <p className="font-mono text-[10px] text-muted">These settings are saved in this browser and sent only when enabled.</p>
    </div>
  );
}

function SessionSidebar({ model }) {
  if (!model) return null;
  const row = (label, value) => (
    <div className="flex justify-between gap-3">
      <span>{label}</span>
      <span className="text-[#e8e2d6]">{value}</span>
    </div>
  );
  return (
    <div className="hidden lg:block w-[220px] shrink-0 border-l border-edge pl-6 py-1">
      <div className="font-mono text-[10px] tracking-widest text-muted/80 uppercase mb-3">session</div>
      <div className="font-mono text-[11px] text-muted space-y-2">
        {row('provider', model.provider)}
        {row('model', model.id)}
        {row('ctx window', model.context_window >= 1_000_000
          ? (model.context_window / 1_000_000) + 'm'
          : Math.round(model.context_window / 1000) + 'k')}
        {row('price /1m', '$' + model.input.toFixed(2) + ' / $' + model.output.toFixed(2))}
      </div>
    </div>
  );
}

function Main({ onLogout }) {
  const [tab, setTab] = useState('ask');
  const [conversationId, setConversationId] = useState(() => {
    try { return sessionStorage.getItem('briefai_active_conversation') || null; } catch { return null; }
  });
  const changeConversation = useCallback((id) => {
    setConversationId(id);
    try { if (id) sessionStorage.setItem('briefai_active_conversation', id); else sessionStorage.removeItem('briefai_active_conversation'); } catch {}
  }, []);
  const openConversation = (id) => { changeConversation(id); setTab('ask'); };

  const [navOpen, setNavOpen] = useState(false);
  const [models, setModels] = useState([]);
  const [model, setModel] = useState('');
  const [rate, setRate] = useState(1);
  const [imageLimits, setImageLimits] = useState(null);
  const [settings, setSettings] = useState(loadSettings);

  useEffect(() => {
    api('/config').then((r) => (r.ok ? r.json() : null)).then((d) => {
      if (!d) return;
      setModels(d.models);
      setModel(d.default);
      setRate(d.usd_pln || 1);
      setImageLimits(d.image_limits || null);
    });
  }, []);

  const currentModel = models.find((m) => m.id === model) || null;

  useEffect(() => {
    try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings)); } catch (e) { /* noop */ }
  }, [settings]);

  const logout = async () => {
    try { await api('/logout', { method: 'POST' }); } catch (e) { /* noop */ }
    onLogout();
  };

  const unauth = () => onLogout();

  const tabBtn = (id, label) => (
    <button
      onClick={() => { setTab(id); setNavOpen(false); }}
      className={
        'font-mono text-xs px-3 py-2 text-left transition-colors border-b '
        + (tab === id ? 'text-accent border-accent' : 'text-muted border-transparent hover:text-white')
      }
    >
      {label}
    </button>
  );

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-10 backdrop-blur bg-ink/85 border-b border-edge">
        <div className="max-w-4xl mx-auto px-5 h-14 flex items-center gap-2">
          <div className="font-mono font-bold text-[15px]">
            brief_ai<span className="text-accent glow">$</span>
          </div>
          <nav className="hidden sm:flex items-center gap-5 ml-8">
            {tabBtn('ask', 'ask')}
            {tabBtn('history', 'history')}
            {tabBtn('shared', 'shared')}
            {tabBtn('settings', 'settings')}
          </nav>
          <button
            onClick={logout}
            className="hidden sm:block ml-auto font-mono text-xs text-muted hover:text-white transition-colors"
          >
            [logout]
          </button>
          <button
            onClick={() => setNavOpen((o) => !o)}
            className="sm:hidden ml-auto font-mono text-sm text-accent"
          >
            [≡]
          </button>
        </div>
        {navOpen && (
          <div className="sm:hidden border-t border-edge px-5 py-2 flex flex-col bg-ink/95">
            {tabBtn('ask', 'ask')}
            {tabBtn('history', 'history')}
            {tabBtn('shared', 'shared')}
            {tabBtn('settings', 'settings')}
            <button
              onClick={logout}
              className="font-mono text-xs text-muted hover:text-white text-left py-2"
            >
              [logout]
            </button>
          </div>
        )}
      </header>

      <main className="max-w-4xl mx-auto px-5 py-6">
        {models.length > 0 && (
          <div className={tab === 'ask' ? 'flex gap-8 items-start' : 'hidden'}>
            <div className="flex-1 min-w-0 max-w-2xl">
              {!conversationId && <ContextControls settings={settings} setSettings={setSettings} onEdit={() => setTab('settings')} />}
              <AskView
                models={models}
                model={model}
                setModel={setModel}
                rate={rate}
                imageLimits={imageLimits}
                settings={settings}
                onUnauth={unauth}
                conversationId={conversationId}
                onConversationChange={changeConversation}
              />
            </div>
            <SessionSidebar model={currentModel} />
          </div>
        )}
        {tab === 'history' && <><ConversationHistory onOpen={openConversation} onUnauth={unauth} /><HistoryView key="history" rate={rate} onUnauth={unauth} /></>}
        {tab === 'shared' && <><ConversationSharedAnswers onUnauth={unauth} onOpen={openConversation} /><HistoryView key="shared" rate={rate} onUnauth={unauth} sharedOnly /></>}
        {tab === 'settings' && <SettingsView settings={settings} setSettings={setSettings} onBack={() => setTab('ask')} />}
      </main>
    </div>
  );
}

function App() {
  const [ready, setReady] = useState(false);
  const [authed, setAuthed] = useState(false);

  const shareMatch = window.location.pathname.match(/^\/s\/([A-Za-z0-9_-]+)/);

  useEffect(() => {
    if (shareMatch) return;
    api('/session')
      .then((r) => r.json())
      .then((d) => { setAuthed(!!d.authenticated); setReady(true); })
      .catch(() => setReady(true));
  }, []);

  if (shareMatch) {
    return <SharedView token={shareMatch[1]} />;
  }

  if (!ready) {
    return <div className="min-h-screen grid place-items-center"><div className="spinner" /></div>;
  }

  return authed
    ? <Main onLogout={() => setAuthed(false)} />
    : <Login onSuccess={() => setAuthed(true)} />;
}

ReactDOM.createRoot(document.getElementById('root')).render(<ImagePreviewProvider><App /></ImagePreviewProvider>);

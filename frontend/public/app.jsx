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

function ShareDialog(props) {
  return props.basePath === '/conversation-messages/' ? <ConversationShareDialog {...props} /> : <LegacyShareDialog {...props} />;
}

function ConversationShareDialog({ itemId, onClose, onChanged, onUnauth }) {
  const [scope, setScope] = useState('conversation');
  const [token, setToken] = useState(null);
  const [created, setCreated] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    let active = true;
    api('/conversation-messages/' + itemId).then(async (res) => {
      if (res.status === 401) { onUnauth(); return; }
      if (!res.ok) throw new Error('Could not load sharing settings.');
      const data = await res.json();
      if (!active) return;
      setToken(data.share_token);
      setCreated(data.snapshot_created_at);
      if (data.share_scope) setScope(data.share_scope);
      setLoaded(true);
    }).catch((e) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [itemId]);
  const save = async (revoke = false) => {
    setBusy(true); setError(''); setCopied(false);
    try {
      const res = await api('/conversation-messages/' + itemId + '/share', {
        method: revoke ? 'DELETE' : token ? 'PATCH' : 'POST',
        ...(!revoke ? { body: JSON.stringify({ scope }) } : {}),
      });
      if (res.status === 401) { onUnauth(); return; }
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not update sharing.');
      setToken(revoke ? null : data.token);
      setCreated(revoke ? null : data.snapshot_created_at);
      onChanged?.(itemId, revoke ? null : data.token);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  const url = token ? location.origin + '/s/' + token : '';
  return <Modal title="Share conversation" onClose={onClose}>
    <p className="text-muted text-sm mb-5">Anyone with the link can read this snapshot. Later messages stay private.</p>
    <fieldset disabled={!loaded || busy} className="space-y-4">
      {[['answer', 'Only this answer'], ['exchange', 'Question and answer'], ['conversation', 'Conversation up to this answer']].map(([value, label]) => <label key={value} className="sharing-option"><input type="radio" name="share-scope" value={value} checked={scope === value} onChange={() => setScope(value)} /><span>{label}</span></label>)}
    </fieldset>
    <p className="text-sm text-muted mt-5">Attachments from included messages are shared too. Deleting an included message disables the link.</p>
    {token && <div className="mt-6 space-y-3">
      {created && <p className="text-xs text-muted">Snapshot: {fmtTime(created)}</p>}
      <label className="text-xs text-muted" htmlFor="snapshot-link">Public link</label>
      <input id="snapshot-link" className="deck-input w-full" value={url} readOnly onFocus={(e) => e.target.select()} />
      <button type="button" className="deck-button" onClick={async () => { try { await navigator.clipboard.writeText(url); setCopied(true); } catch { setError('Select and copy the link above.'); } }}>{copied ? 'Copied' : 'Copy link'}</button>
      <a className="text-accent text-sm ml-4" href={url} target="_blank" rel="noopener noreferrer">Preview ↗</a>
    </div>}
    {error && <p role="alert" className="text-bad text-sm mt-4">{error}</p>}
    <div className="flex flex-wrap gap-3 mt-6"><button type="button" className="deck-button primary" disabled={!loaded || busy} onClick={() => save()}>{busy ? 'Saving…' : token ? 'Update snapshot' : 'Publish link'}</button>{token && <button type="button" className="deck-button text-bad" disabled={busy} onClick={() => save(true)}>Stop sharing</button>}</div>
  </Modal>;
}

function LegacyShareDialog({ itemId, onClose, onChanged, onUnauth, basePath = '/history/' }) {
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

        {data.messages ? <div className="space-y-4">
          {data.messages.map((message, index) => <section key={index} className={'corner-panel border border-edge p-5 ' + (message.role === 'user' ? 'bg-panel2' : 'bg-panel')}>
            <div className="font-mono text-[11px] text-muted mb-4 flex flex-wrap gap-3"><span>{message.role === 'user' ? 'You' : message.model_label}</span><time dateTime={message.created_at}>{fmtTime(message.created_at)}</time></div>
            {message.role === 'user' ? <div className="whitespace-pre-wrap break-words">{message.content}</div> : <Markdown text={message.content} />}
            {!!message.images?.length && <div className="mt-3"><AttachmentStrip images={message.images} size={76} base={'/api/share/' + token + '/images/'} /></div>}
          </section>)}
        </div> : <div className="corner-panel bg-panel border border-edge rounded-2xl p-5">
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
        </div>}
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
          <img src={image.previewUrl || imageUrl(image.sha, image.has_thumb)} alt="" className="w-full h-full object-cover" />
        </button>
        {onRemove && <button
          type="button"
          onClick={() => onRemove(image.id)}
          title="Remove"
          aria-label={`Remove image ${index + 1}`}
          className="absolute -top-1 -right-1 w-4 h-4 bg-ink border border-edge flex items-center justify-center text-muted hover:text-white"
        >
          <svg width="8" height="8" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.6"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>}
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

function ConversationWorkspace({ models, defaultModel, rate, imageLimits, settings, onUnauth, visible, openRequest, onModelChange, actionsRef, onReady }) {
  const [workspace, setWorkspace] = useState(null);
  const [storageError, setStorageError] = useState(false);
  const [closeKey, setCloseKey] = useState(null);
  const storageRef = useRef(null);
  const workspaceRef = useRef(null);
  const visibleRef = useRef(visible);
  const positioning = useRef(false);
  const elementRef = useRef(null);
  const transitionLayer = useRef(null);
  const transitions = useRef(null);
  visibleRef.current = visible;
  const publish = useCallback((next) => {
    const previous = workspaceRef.current;
    if (previous && previous.active !== next.active && visibleRef.current) {
      const oldIndex = previous.views.findIndex((view) => view.key === previous.active);
      const newIndex = previous.views.findIndex((view) => view.key === next.active);
      const direction = newIndex < 0 || newIndex > oldIndex ? 1 : -1;
      const outgoing = elementRef.current?.querySelector('[data-conversation-view]:not([hidden])');
      transitions.current?.capture(outgoing, direction);
    }
    workspaceRef.current = next;
    setWorkspace(next);
    storageRef.current.setOrder(next.views, next.active);
  }, []);
  useEffect(() => {
    let cancelled = false;
    const storage = new BriefSessions.WorkspaceStorage(() => setStorageError(true));
    storageRef.current = storage;
    storage.restore().then((value) => {
      if (cancelled) return;
      for (const view of value.views) {
        if (!view.model) view.model = defaultModel;
        storage.save(view).catch(() => {});
      }
      publish(value);
    });
    return () => { cancelled = true; workspaceRef.current = null; storage.dispose(); };
  }, []);

  const updateView = useCallback((key, patch) => {
    const current = workspaceRef.current;
    if (!current) return Promise.resolve();
    const view = current.views.find((item) => item.key === key);
    if (!view || Object.keys(patch).every((name) => view[name] === patch[name])) return Promise.resolve();
    const updated = { ...view, ...patch };
    publish({ ...current, views: current.views.map((item) => item.key === key ? updated : item) });
    return storageRef.current.save(updated).catch(() => {});
  }, [publish]);

  const rememberPosition = useCallback(() => {
    const current = workspaceRef.current;
    if (!current || !visibleRef.current || positioning.current) return current;
    const view = current.views.find((v) => v.key === current.active);
    const atBottom = document.documentElement.scrollHeight - innerHeight - scrollY < 80;
    const updated = { ...view, scroll: scrollY, atBottom, unread: atBottom ? false : view.unread };
    storageRef.current.save(updated).catch(() => {});
    const next = { ...current, views: current.views.map((v) => v.key === updated.key ? updated : v) };
    workspaceRef.current = next;
    return next;
  }, []);
  const select = useCallback((key) => {
    const current = rememberPosition();
    if (!current || current.active === key) return;
    const leaving = current.views.find((view) => view.key === current.active);
    const empty = leaving && !leaving.conversationId && !leaving.prompt && !leaving.images.length && !leaving.pendingRequest;
    const views = empty ? current.views.filter((view) => view.key !== leaving.key) : current.views;
    publish({ ...current, views, active: key });
    if (empty) storageRef.current.remove(leaving.key).catch(() => {});
  }, [publish, rememberPosition]);

  const insert = useCallback((view) => {
    const current = rememberPosition();
    const leaving = current.views.find((item) => item.key === current.active);
    const empty = leaving && !leaving.conversationId && !leaving.prompt && !leaving.images.length && !leaving.pendingRequest;
    const insertion = current.views.findIndex((item) => item.key === current.active) + (empty ? 0 : 1);
    const views = empty ? current.views.filter((item) => item.key !== leaving.key) : [...current.views];
    views.splice(insertion, 0, view);
    if (empty) storageRef.current.remove(leaving.key).catch(() => {});
    storageRef.current.save(view).catch(() => {});
    publish({ views, active: view.key });
  }, [publish, rememberPosition]);

  const newConversation = useCallback(() => {
    const current = workspaceRef.current;
    const empty = current.views.find((v) => !v.conversationId && !v.prompt && !v.images.length && !v.pendingRequest);
    if (empty && empty.key === current.active) {
      const panel = elementRef.current?.querySelector('[data-conversation-view]:not([hidden])');
      if (panel && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
        panel.getAnimations().forEach((animation) => animation.cancel());
        panel.animate([
          { transform: 'scale(1)' },
          { transform: 'scale(1.018)', offset: 0.45 },
          { transform: 'scale(1)' },
        ], { duration: 300, easing: 'ease-in-out' });
      }
    } else if (empty) select(empty.key);
    else insert(BriefSessions.freshView(null, defaultModel));
  }, [defaultModel, insert, select]);

  const remove = useCallback((key) => {
    const current = workspaceRef.current;
    if (!current) return;
    const index = current.views.findIndex((v) => v.key === key);
    const views = current.views.filter((v) => v.key !== key);
    let active = current.active;
    if (!views.length) {
      views.push(BriefSessions.freshView(null, defaultModel));
      storageRef.current.save(views[0]).catch(() => {});
    }
    if (active === key) active = views[Math.min(index, views.length - 1)].key;
    publish({ views, active });
    storageRef.current.remove(key).catch(() => {});
    setCloseKey(null);
  }, [defaultModel, publish]);

  const changeConversation = useCallback((key, id) => {
    if (!id) { remove(key); return; }
    const current = workspaceRef.current;
    if (!current?.views.some((view) => view.key === key)) return;
    const existing = current.views.find((view) => view.key !== key && view.conversationId === id);
    if (existing) {
      const wasActive = current.active === key;
      remove(key);
      if (wasActive) select(existing.key);
      return;
    }
    updateView(key, { conversationId: id });
  }, [remove, select, updateView]);

  useEffect(() => {
    if (!workspace || !openRequest) return;
    const current = workspaceRef.current;
    const existing = current.views.find((view) => view.conversationId === openRequest.id);
    if (existing) select(existing.key);
    else insert(BriefSessions.freshView(openRequest.id));
  }, [openRequest, !!workspace, defaultModel, insert, select]);

  useEffect(() => {
    let timer;
    const savePosition = () => {
      if (positioning.current || !visibleRef.current || !workspaceRef.current) return;
      const current = workspaceRef.current;
      const atBottom = document.documentElement.scrollHeight - innerHeight - scrollY < 80;
      const view = current.views.find((v) => v.key === current.active);
      updateView(current.active, { scroll: scrollY, atBottom, unread: atBottom ? false : view.unread });
    };
    const onScroll = () => { clearTimeout(timer); timer = setTimeout(savePosition, 100); };
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('pagehide', savePosition);
    window.addEventListener('briefai:before-navigation', savePosition);
    return () => { clearTimeout(timer); window.removeEventListener('scroll', onScroll); window.removeEventListener('pagehide', savePosition); window.removeEventListener('briefai:before-navigation', savePosition); };
  }, [updateView]);

  useEffect(() => {
    if (!workspace || !elementRef.current) return;
    transitions.current = new BriefNavigation.Transitions(transitionLayer.current);
    const detach = BriefNavigation.gestures(elementRef.current, () => visibleRef.current, (direction) => {
      const current = workspaceRef.current;
      const index = current.views.findIndex((view) => view.key === current.active);
      const neighbor = current.views[index + direction];
      if (neighbor) select(neighbor.key);
    });
    return () => { detach(); transitions.current?.cancel(); };
  }, [!!workspace, select]);
  useEffect(() => {
    if (visible) return;
    transitions.current?.cancel();
    const current = workspaceRef.current;
    if (!current) return;
    const leaving = current.views.find((view) => view.key === current.active);
    if (leaving && current.views.length > 1 && !leaving.conversationId && !leaving.prompt && !leaving.images.length && !leaving.pendingRequest) remove(leaving.key);
  }, [visible, remove]);

  const active = workspace?.views.find((v) => v.key === workspace.active);
  const messageCount = active?.conversation?.messages?.length || 0;
  React.useLayoutEffect(() => {
    if (!visible || !active) return;
    positioning.current = true;
    const frame = requestAnimationFrame(() => {
      const target = active.atBottom && messageCount ? document.documentElement.scrollHeight : active.scroll;
      window.scrollTo(0, target);
      transitions.current?.play(elementRef.current?.querySelector('[data-conversation-view]:not([hidden])'));
      if (active.atBottom && active.unread) updateView(active.key, { unread: false });
      requestAnimationFrame(() => { positioning.current = false; });
    });
    return () => cancelAnimationFrame(frame);
  }, [workspace?.active, visible, messageCount]);

  useEffect(() => { if (active) onModelChange(active.model || defaultModel); }, [active?.model, defaultModel, onModelChange]);
  // Refresh the lifetime of an open session without persisting its contents again.
  useEffect(() => {
    const timer = setInterval(() => {
      const current = workspaceRef.current;
      if (current) storageRef.current.save(current.views[0]).catch(() => {});
    }, 60000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!workspace) return;
    actionsRef.current = {
      newConversation,
      close() {
        const current = workspaceRef.current;
        const view = current.views.find((item) => item.key === current.active);
        if (view.prompt || view.images.length || view.pendingRequest) setCloseKey(view.key);
        else remove(view.key);
      },
    };
    onReady(true);
    return () => { actionsRef.current = null; onReady(false); };
  }, [!!workspace, newConversation, remove, actionsRef, onReady]);
  if (!workspace) return <p role="status" className="text-sm text-muted">Restoring workspace…</p>;
  const index = workspace.views.findIndex((v) => v.key === workspace.active);
  const previous = workspace.views[index - 1];
  const next = workspace.views[index + 1];
  const label = (view) => view.conversation?.title || view.conversation?.messages?.find((m) => m.role === 'user')?.content?.slice(0, 55) || view.prompt.slice(0, 55) || 'New conversation';
  const status = (view) => view.unread ? 'New reply' : ['queued', 'running'].includes(view.conversation?.job?.status) ? 'Generating…' : null;
  const requestClose = () => {
    if (active.prompt || active.images.length || active.pendingRequest) setCloseKey(active.key);
    else remove(active.key);
  };
  return <div className="conversation-workspace" ref={elementRef}>
    {storageError && <p role="alert" className="text-sm text-bad mb-4">Draft changes could not be saved locally. Keep this tab open to retain them.</p>}
    <nav aria-label="Open conversations" className="conversation-navigation">
      <div>{previous && <button type="button" className="conversation-neighbor" aria-label={'Previous conversation: ' + label(previous)} onClick={() => select(previous.key)}><span aria-hidden="true">←</span><span className="min-w-0 break-words">{label(previous)}{status(previous) && <small className="block text-accent2">{status(previous)}</small>}</span></button>}</div>
      <div>{next && <button type="button" className="conversation-neighbor next" aria-label={'Next conversation: ' + label(next)} onClick={() => select(next.key)}><span className="min-w-0 break-words">{label(next)}{status(next) && <small className="block text-accent2">{status(next)}</small>}</span><span aria-hidden="true">→</span></button>}</div>
    </nav>
    <div className="conversation-stage">
    {workspace.views.map((view) => <div key={view.key} data-conversation-view={view.key} data-conversation-id={view.conversationId || ''} hidden={view.key !== workspace.active}>
      <AskView models={models} model={view.model || defaultModel} setModel={(model) => updateView(view.key, { model })}
        rate={rate} imageLimits={imageLimits} settings={settings} onUnauth={onUnauth}
        conversationId={view.conversationId} onConversationChange={changeConversation}
        viewKey={view.key} viewState={view} onViewState={updateView} active={visible && view.key === workspace.active}
        onDeleted={remove} onBranch={(id) => insert(BriefSessions.freshView(id, view.model || defaultModel))} onClose={requestClose} />
    </div>)}
    </div>
    <div ref={transitionLayer} className="conversation-transition-layer" aria-hidden="true" />
    {active.unread && <button type="button" className="new-reply-button" onClick={() => { window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'smooth' }); updateView(active.key, { atBottom: true, unread: false }); }}>New reply ↓</button>}
    {closeKey && <Modal title="Discard draft?" onClose={() => setCloseKey(null)}><p className="text-sm text-muted">Closing this view discards its unsent text and attachments. Sent messages remain in History.</p><div className="flex gap-3 mt-6"><button type="button" className="deck-button" onClick={() => setCloseKey(null)}>Keep open</button><button type="button" className="deck-button danger" onClick={() => remove(closeKey)}>Discard and close</button></div></Modal>}
  </div>;
}

function MessageIcon({ name }) {
  const paths = {
    edit: <path d="m15 4 5 5M4 20l4-1L20 7a2.1 2.1 0 0 0-3-3L5 16z" />,
    copy: <><rect x="8" y="8" width="12" height="13" rx="2" /><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h3" /></>,
    share: <path d="M12 15V3m-4 4 4-4 4 4M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7" />,
    branch: <><path d="M5 20v-7c0-4 4-4 4-8V3m-3 3 3-3 3 3" /><path d="M5 13c0-4 13 0 13-8V3m-3 3 3-3 3 3" strokeDasharray="2.5 3" /></>,
    regenerate: <path d="M20 10a8 8 0 1 0-1 7M20 4v6h-6" />,
    close: <path d="m6 6 12 12M6 18 18 6" />,
    check: <path d="m5 12 4 4L19 6" />,
    attach: <path d="m8 12 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l9-9m-7 13 8-8" />,
  };
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

function AttachmentPicker({ images, limits, onClick, disabled, label = 'Attach images' }) {
  const bytes = images.reduce((sum, im) => sum + (im.sizeBytes || 0), 0);
  const [details, setDetails] = useState(false);
  useEffect(() => { setDetails(false); }, [images]);
  return <button type="button" className={'attachment-picker' + (details ? ' show-details' : '')} aria-label={label} disabled={disabled}
    onMouseEnter={() => setDetails(true)} onMouseLeave={() => setDetails(false)} onBlur={() => setDetails(false)}
    onFocus={(e) => { if (e.currentTarget.matches(':focus-visible')) setDetails(true); }}
    onPointerDown={() => setDetails(false)} onClick={(e) => { setDetails(false); onClick(e); }}>
    <MessageIcon name="attach" />{images.length > 0 && <span>{images.length}</span>}
    <span className="attachment-details">{images.length} of {limits.max_images} images<br />{(bytes / 1e6).toFixed(1)} of {limits.max_total_mb} MB<br />PNG, JPEG, WebP, GIF</span>
  </button>;
}

function ModelSelect({ models, value, onChange, disabled = false, label = 'Model' }) {
  return <select aria-label={label} className="model-select" value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
    {!models.some((m) => m.id === value) && <option value={value}>{value} · unavailable</option>}
    {groupByProvider(models).map((group) => <optgroup key={group.label} label={group.label}>{group.items.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}</optgroup>)}
  </select>;
}

function MessageEditor({ edit, models, limits, onSave, onCancel }) {
  const [content, setContent] = useState(edit.content);
  const [images, setImages] = useState(edit.images);
  const [model, setModel] = useState(edit.model);
  const [saving, setSaving] = useState(false);
  const [reading, setReading] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState('');
  const fileInput = useRef(null);
  const textarea = useRef(null);
  const pending = useRef(null);
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);
  React.useLayoutEffect(() => {
    const input = textarea.current;
    input.focus({ preventScroll: true });
    input.setSelectionRange(input.value.length, input.value.length);
    input.scrollTop = input.scrollHeight;
  }, []);
  const user = edit.role === 'user';
  const locked = saving || uncertain;
  const selected = models.find((m) => m.id === model);
  const addFiles = async (files) => {
    if (!user || locked || reading) return;
    const allowed = Array.from(files || []).filter((f) => limits.allowed_types.includes(f.type));
    if (!allowed.length) return;
    setReading(true);
    try {
      const added = await Promise.all(allowed.map(readImageFile));
      if (!mounted.current) return;
      const next = [...images, ...added];
      if (added.some((im) => im.sizeBytes > limits.max_image_mb * 1e6) || next.length > limits.max_images || next.reduce((sum, im) => sum + im.sizeBytes, 0) > limits.max_total_mb * 1e6) {
        setError('The selected images exceed the attachment limits.'); return;
      }
      setImages(next); setError('');
    } catch { if (mounted.current) setError('Could not read one of the images.'); }
    finally { if (mounted.current) setReading(false); }
  };
  const save = async () => {
    if (saving || reading || (!content.trim() && (!user || !images.length))) return;
    if (!pending.current) pending.current = {
      content, revision: edit.revision, request_id: crypto.randomUUID(),
      ...(user ? { model, images: images.map((im) => im.sha ? { sha: im.sha } : {
        data: im.base64, media_type: im.mediaType, width: im.width, height: im.height,
        ...(im.thumb ? { thumb: im.thumb.base64, thumb_media_type: im.thumb.mediaType } : {}),
      }) } : {}),
    };
    setSaving(true); setError('');
    try { await onSave(pending.current); }
    catch (e) {
      if (!mounted.current) return;
      if (e.definitive) { pending.current = null; setUncertain(false); }
      else setUncertain(true);
      setError(e.message);
    } finally { if (mounted.current) setSaving(false); }
  };
  return <div className="message-editor" onDrop={(e) => { e.preventDefault(); addFiles(e.dataTransfer.files); }} onDragOver={(e) => e.preventDefault()}>
    <textarea ref={textarea} aria-label="Edit message" value={content} disabled={locked} maxLength={100000} rows={4}
      className="w-full bg-transparent resize-y outline-none px-5 pt-5 pb-2 text-[0.95rem]"
      onChange={(e) => setContent(e.target.value)}
      onPaste={(e) => { const files = Array.from(e.clipboardData?.items || []).filter((item) => item.type.startsWith('image/')).map((item) => item.getAsFile()).filter(Boolean); if (files.length) addFiles(files); }}
      onKeyDown={(e) => { if (e.key === 'Escape' && !saving) { e.preventDefault(); onCancel(); } else if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); save(); } }} />
    {images.length > 0 && <div className="flex flex-wrap gap-3 px-5 pb-4">{images.map((im, index) => <ImageThumb key={im.id} image={im} images={images} index={index} onRemove={locked || !user ? null : (id) => setImages((previous) => previous.filter((item) => item.id !== id))} />)}</div>}
    <div className="flex flex-wrap gap-3 items-center px-5 pb-4">
      {user && <><ModelSelect models={models} value={model} disabled={locked} onChange={setModel} label="Edit model" /><AttachmentPicker images={images} limits={limits} label="Attach images to message" disabled={locked || reading || selected?.supports_images === false} onClick={() => fileInput.current.click()} />
      <input ref={fileInput} type="file" multiple accept={limits.allowed_types.join(',')} className="hidden" onChange={(e) => { addFiles(e.target.files); e.target.value = ''; }} /></>}
      <div className="ml-auto flex gap-2 items-center"><button type="button" className="message-action" aria-label="Cancel editing" title="Cancel · Esc" disabled={saving} onClick={onCancel}><MessageIcon name="close" /></button>
      <button type="button" className="flex items-center gap-2 bg-accent hover:bg-accent2 disabled:opacity-40 disabled:hover:bg-accent text-accentInk font-mono text-[11px] font-bold px-4 py-1.5 transition-colors" disabled={saving || reading || (!content.trim() && (!user || !images.length))} onClick={save}>{saving ? 'Saving…' : uncertain ? 'Try again' : 'Save'}</button></div>
      <p className="w-full text-[11px] font-mono text-muted">Saving changes removes later messages{user ? ' and generates a new answer.' : '.'}</p>
      {error && <p role="alert" className="w-full text-sm text-bad">{error}</p>}
    </div>
  </div>;
}

function RegeneratePopover({ selection, models, onConfirm, onClose }) {
  const [model, setModel] = useState(selection.model);
  const [saving, setSaving] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState('');
  const [position, setPosition] = useState(null);
  const element = useRef(null);
  const pending = useRef(null);
  const savingRef = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    const place = () => {
      if (!element.current) return;
      const box = element.current.getBoundingClientRect();
      const scale = box.width / element.current.offsetWidth;
      const anchor = selection.anchor.isConnected ? selection.anchor.getBoundingClientRect() : selection.rect;
      const left = Math.max(12, Math.min(innerWidth - box.width - 12, anchor.right - box.width));
      const proposed = anchor.bottom + 8 + box.height <= innerHeight - 12 ? anchor.bottom + 8 : anchor.top - box.height - 8;
      const top = Math.max(12, Math.min(innerHeight - box.height - 12, proposed));
      setPosition({ left: left / scale, top: top / scale, maxHeight: (innerHeight - top - 12) / scale });
    };
    const outside = (event) => { if (!savingRef.current && !element.current?.contains(event.target) && !selection.anchor.contains(event.target)) onClose(); };
    const escape = (event) => { if (event.key === 'Escape' && !savingRef.current) { event.preventDefault(); onClose(); } };
    const dismiss = (event) => {
      if (savingRef.current) return;
      if (event.type === 'resize') { onClose(); return; }
      if (element.current?.contains(event.target)) return;
      const anchor = selection.anchor.getBoundingClientRect();
      if (Math.abs(anchor.top - selection.rect.top) > 1 || Math.abs(anchor.left - selection.rect.left) > 1) onClose();
    };
    place();
    element.current?.querySelector('select')?.focus({ preventScroll: true });
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    window.addEventListener('resize', dismiss);
    document.addEventListener('scroll', dismiss, true);
    return () => {
      mounted.current = false;
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape);
      window.removeEventListener('resize', dismiss);
      document.removeEventListener('scroll', dismiss, true);
      if (selection.anchor.isConnected) selection.anchor.focus({ preventScroll: true });
    };
  }, []);
  const confirm = async () => {
    if (savingRef.current) return;
    if (!pending.current) pending.current = { model, revision: selection.revision, request_id: crypto.randomUUID() };
    savingRef.current = true; setSaving(true); setError('');
    try { await onConfirm(pending.current); }
    catch (e) {
      if (!mounted.current) return;
      if (e.definitive) { pending.current = null; setUncertain(false); } else setUncertain(true);
      setError(e.message);
    } finally { savingRef.current = false; if (mounted.current) setSaving(false); }
  };
  return ReactDOM.createPortal(<section ref={element} className="regenerate-popover" role="dialog" aria-label="Regenerate answer" style={{ ...position, visibility: position ? 'visible' : 'hidden' }}>
    <h3 className="font-mono text-[12px] mb-3">Regenerate answer</h3>
    <ModelSelect models={models} value={model} onChange={setModel} disabled={saving || uncertain} label="Reply model" />
    <p className="font-mono text-[10px] text-muted mt-3">This answer and all later messages will be removed.</p>
    {error && <p role="alert" className="text-xs text-bad mt-3">{error}</p>}
    <div className="flex items-center justify-between gap-3 mt-4"><button type="button" className="message-action" aria-label="Cancel regeneration" title="Cancel" disabled={saving} onClick={onClose}><MessageIcon name="close" /></button><button type="button" className="deck-button primary gap-2" disabled={saving || !models.some((m) => m.id === model)} onClick={confirm}><MessageIcon name="regenerate" />{saving ? 'Regenerating…' : uncertain ? 'Try again' : 'Regenerate'}</button></div>
  </section>, document.body);
}

function AskView({ models, model, setModel, rate, imageLimits, settings, onUnauth, conversationId, onConversationChange, viewKey, viewState, onViewState, active, onDeleted, onBranch, onClose }) {
  const limits = imageLimits || DEFAULT_IMAGE_LIMITS;
  const currentModel = models.find((m) => m.id === model) || null;
  const [prompt, setPrompt] = useState(viewState.prompt);
  const [images, setImages] = useState(viewState.images);
  const [imageError, setImageError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [shareMessage, setShareMessage] = useState(null);
  const [branching, setBranching] = useState(null);
  const branchRequests = useRef(new Map());
  const [conversation, setConversation] = useState(viewState.conversation);
  const [restoring, setRestoring] = useState(!!conversationId && !viewState.conversation);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [deleting, setDeleting] = useState(false);
  const [deleteMessage, setDeleteMessage] = useState(null);
  const [editing, setEditing] = useState(null);
  const [savingEdit, setSavingEdit] = useState(false);
  const [regenerate, setRegenerate] = useState(null);
  const [regenerating, setRegenerating] = useState(false);
  const [copied, setCopied] = useState(null);
  const fileInputRef = useRef(null);
  const requestRef = useRef(viewState.pendingRequest ? { id: viewState.pendingRequest.id, body: null } : null);
  const latestView = useRef({ active, viewState });
  latestView.current = { active, viewState };
  const previousConversation = useRef(viewState.conversation);
  const modelChosen = useRef(!!viewState.model);
  const changeConversation = useCallback((id) => onConversationChange(viewKey, id), [viewKey, onConversationChange]);
  useEffect(() => { onViewState(viewKey, { prompt, images, conversation }); }, [viewKey, prompt, images, conversation, onViewState]);
  const unauthRef = useRef(onUnauth);
  unauthRef.current = onUnauth;
  const busy = ['queued', 'running'].includes(conversation?.job?.status);
  const loading = submitting || busy || savingEdit || !!editing || regenerating || !!regenerate;
  const unanswered = conversation?.messages?.at(-1)?.role === 'user';

  useEffect(() => {
    let cancelled = false;
    let timer;
    setRestoring(!!conversationId && !previousConversation.current);
    setError('');
    const poll = async () => {
      try {
        const res = await api('/conversations/' + conversationId);
        if (cancelled) return;
        if (res.status === 401) { unauthRef.current(); return; }
        if (res.status === 404) {
          const draft = latestView.current.viewState;
          if (draft.prompt || draft.images.length) {
            previousConversation.current = null;
            requestRef.current = null;
            setConversation(null); setRestoring(false);
            onViewState(viewKey, { conversationId: null, conversation: null, pendingRequest: null });
          } else onDeleted(viewKey);
          return;
        }
        if (!res.ok) throw new Error('Could not load this conversation.');
        const data = await res.json();
        if (cancelled) return;
        const previous = previousConversation.current;
        const wasPending = ['queued', 'running'].includes(previous?.job?.status) || !!latestView.current.viewState.pendingRequest;
        const last = data.messages.at(-1);
        if (wasPending && last?.role === 'assistant' && last.id !== previous?.messages?.at(-1)?.id) {
          const current = latestView.current;
          if (!current.active || !current.viewState.atBottom) onViewState(viewKey, { unread: true });
        }
        previousConversation.current = data;
        setConversation(data);
        if (!modelChosen.current) {
          const lastModel = data.messages.filter((m) => m.role === 'user').at(-1)?.model;
          if (models.some((m) => m.id === lastModel)) setModel(lastModel);
          modelChosen.current = true;
        }
        setRestoring(false);
        setError('');
        if (['queued', 'running'].includes(data.job?.status) || ['queued', 'running'].includes(data.title_status)) timer = setTimeout(poll, 1200);
      } catch (e) {
        if (!cancelled) { setError('Connection lost. Reconnecting…'); timer = setTimeout(poll, 3000); }
      }
    };
    if (conversationId) poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [conversationId, refresh, onDeleted, onViewState, viewKey]);

  useEffect(() => {
    const pending = viewState.pendingRequest;
    if (!pending) return;
    let cancelled = false;
    let timer;
    const recover = async () => {
      try {
        const res = await api('/conversation-requests/' + pending.id);
        if (cancelled) return;
        if (res.status === 401) { unauthRef.current(); return; }
        if (res.ok) {
          const data = await res.json();
          if (cancelled) return;
          changeConversation(data.id);
          setPrompt(''); setImages([]);
          requestRef.current = null;
          onViewState(viewKey, { pendingRequest: null });
          setRefresh((value) => value + 1);
        } else if (res.status !== 404) throw new Error('Recovery failed');
      } catch {
        if (!cancelled) { setError('Could not check the last submission. Reconnecting…'); timer = setTimeout(recover, 3000); }
      }
    };
    recover();
    return () => { cancelled = true; clearTimeout(timer); };
  }, []);

  const branch = async (messageId) => {
    if (branching) return;
    setBranching(messageId); setError('');
    if (!branchRequests.current.has(messageId)) branchRequests.current.set(messageId, crypto.randomUUID());
    try {
      const res = await api('/conversations/' + conversationId + '/branches', {
        method: 'POST', body: JSON.stringify({ message_id: messageId, request_id: branchRequests.current.get(messageId) }),
      });
      if (res.status === 401) { onUnauth(); return; }
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not create branch.');
      branchRequests.current.delete(messageId);
      onBranch(data.id);
    } catch (e) { setError(e.message); }
    finally { setBranching(null); }
  };

  const retry = async () => {
    setSubmitting(true); setError('');
    try {
      const res = await api('/conversations/' + conversationId + '/retry', { method: 'POST' });
      if (res.status === 401) { unauthRef.current(); return; }
      const data = await res.json();
      if (res.status === 409) { setRefresh((x) => x + 1); return; }
      if (!res.ok) throw new Error(data.detail || data.error || 'Could not retry.');
      setRefresh((x) => x + 1);
    } catch (e) { setError(e.message); setRefresh((x) => x + 1); }
    finally { setSubmitting(false); }
  };

  useEffect(() => { if (!active) { setEditing(null); setRegenerate(null); } }, [active]);

  const startEditing = (message) => {
    const index = conversation.messages.findIndex((m) => m.id === message.id);
    const answer = conversation.messages[index + 1];
    setEditing({ id: message.id, role: message.role, content: message.content,
      images: (message.images || []).map((im, index) => ({ ...im, id: im.sha + '-' + index, sizeBytes: im.byte_size, mediaType: im.media_type })),
      revision: conversation.revision, model: answer?.role === 'assistant' ? answer.model : message.model });
  };

  const saveEdit = async (body) => {
    setSavingEdit(true); setError('');
    try {
      const res = await api('/conversations/' + conversationId + '/messages/' + editing.id, { method: 'PATCH', body: JSON.stringify(body) });
      if (res.status === 401) { onUnauth(); return; }
      const data = await res.json();
      if (!res.ok) { const error = new Error(data.detail || data.error || 'Could not save the message.'); error.definitive = res.status < 500; throw error; }
      if (data.changed && body.model) setModel(body.model);
      branchRequests.current.clear();
      setEditing(null); setRefresh((x) => x + 1);
    } catch (e) { setRefresh((x) => x + 1); throw e; }
    finally { setSavingEdit(false); }
  };

  const confirmRegenerate = async (body) => {
    setRegenerating(true); setError('');
    try {
      const res = await api('/conversations/' + conversationId + '/messages/' + regenerate.id + '/regenerate', { method: 'POST', body: JSON.stringify(body) });
      if (res.status === 401) { onUnauth(); return; }
      const data = await res.json();
      if (!res.ok) { const error = new Error(data.detail || data.error || 'Could not regenerate the answer.'); error.definitive = res.status < 500; throw error; }
      setModel(body.model); setRegenerate(null); branchRequests.current.clear(); setRefresh((x) => x + 1);
    } catch (e) { setRefresh((x) => x + 1); throw e; }
    finally { setRegenerating(false); }
  };

  const truncate = async () => {
    setDeleting(true); setError('');
    try {
      const res = await api('/conversations/' + conversationId + '/messages/' + deleteMessage, { method: 'DELETE' });
      if (res.status === 401) { unauthRef.current(); return; }
      const data = await res.json();
      if (!res.ok) throw new Error('Could not delete the message.');
      setDeleteMessage(null);
      if (data.deleted_conversation) onDeleted(viewKey);
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
    if (loading || restoring || (unanswered && !requestRef.current) || (!requestRef.current?.body && ((!prompt.trim() && !images.length) || overLimit || unsupportedImages))) return;
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
    else if (!requestRef.current.body) requestRef.current.body = body;
    const submission = requestRef.current;
    await onViewState(viewKey, { pendingRequest: { id: submission.id } });
    try {
      const res = await api(conversationId ? '/conversations/' + conversationId + '/messages' : '/conversations', {
        method: 'POST', body: JSON.stringify({ ...submission.body, request_id: submission.id }),
      });
      if (res.status === 401) { onUnauth(); return; }
      const data = await res.json();
      if (!res.ok) {
        if (res.status < 500) {
          requestRef.current = null;
          onViewState(viewKey, { pendingRequest: null });
        }
        throw new Error(data.detail || data.error || 'Request failed.');
      }
      changeConversation(data.id);
      setRefresh((x) => x + 1);
      setPrompt(''); setImages([]); setImageError('');
      requestRef.current = null;
      onViewState(viewKey, { pendingRequest: null });
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

  const sendDisabled = loading || restoring || unanswered || (!prompt.trim() && !images.length) || overLimit || unsupportedImages;

  return (
    <div className="space-y-4">
      {shareMessage && <ShareDialog itemId={shareMessage} basePath="/conversation-messages/" onClose={() => setShareMessage(null)} onUnauth={onUnauth} />}
      {restoring && <p className="text-sm text-muted" role="status">Loading conversation…</p>}
      {conversation?.messages.map((message) => (
        <div key={message.id} className={'corner-panel border border-edge ' + (message.role === 'user' ? 'bg-panel2' : 'bg-panel')}>
          <div className="message-toolbar flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2 border-b border-edge font-mono text-[11px] text-muted">
            <span>{message.role === 'user' ? 'You' : models.find((m) => m.id === message.model)?.label || message.model}</span>
            <time dateTime={message.created_at}>{fmtTime(message.created_at)}</time>
            {message.edited_at && <span title={message.edited_at}>edited</span>}
            <div className="message-actions ml-auto flex items-center gap-1">
              <button type="button" className="message-action" aria-label="Copy" title={copied === message.id ? 'Copied' : 'Copy'} onClick={async () => { try { await navigator.clipboard.writeText(message.content); setCopied(message.id); setTimeout(() => setCopied(null), 1500); } catch { setError('Clipboard is unavailable. Select the message to copy it.'); } }}><MessageIcon name={copied === message.id ? 'check' : 'copy'} /></button>
              <button type="button" className="message-action" aria-label="Edit" title="Edit" disabled={submitting || deleting || !!editing || !!regenerate || regenerating} onClick={() => startEditing(message)}><MessageIcon name="edit" /></button>
              {message.role === 'user' ? <button type="button" className="message-action" aria-label="Delete message and later replies" title="Delete message and later replies" disabled={deleting || !!editing || !!regenerate} onClick={() => setDeleteMessage(message.id)}><TrashIcon /></button> : <>
                <button type="button" className="message-action" aria-label="Regenerate" title="Regenerate" disabled={!!editing || !!regenerate || submitting || regenerating} onClick={(e) => setRegenerate({ id: message.id, model: message.model, revision: conversation.revision, anchor: e.currentTarget, rect: e.currentTarget.getBoundingClientRect() })}><MessageIcon name="regenerate" /></button>
                <button type="button" className="message-action" aria-label="Branch" title={branching === message.id ? 'Creating branch…' : 'Branch'} disabled={!!branching || !!editing || !!regenerate} onClick={() => branch(message.id)}><MessageIcon name="branch" /></button>
                <button type="button" className="message-action" aria-label="Share" title="Share" disabled={!!editing || !!regenerate} onClick={() => setShareMessage(message.id)}><MessageIcon name="share" /></button>
              </>}
            </div>
          </div>
          {editing?.id === message.id ? <MessageEditor key={editing.id} edit={editing} models={models} limits={limits} onSave={saveEdit} onCancel={() => setEditing(null)} /> : <div className="p-5">
            {message.role === 'user' ? <div className="whitespace-pre-wrap break-words">{message.content}</div> : <><Meta result={{ ...message, model_label: models.find((m) => m.id === message.model)?.label || message.model }} rate={rate} /><Markdown text={message.content} /></>}
            {!!message.images?.length && <div className="mt-3"><AttachmentStrip images={message.images} size={64} /></div>}
          </div>}
        </div>
      ))}
      {regenerate && <RegeneratePopover key={regenerate.id} selection={regenerate} models={models} onConfirm={confirmRegenerate} onClose={() => setRegenerate(null)} />}
      {conversation?.job?.status === 'failed' && unanswered && <div role="alert" className="border border-bad/40 p-4 text-sm flex flex-wrap items-center justify-between gap-3"><p>{conversation.job.error_detail}</p><button type="button" className="deck-button gap-2" onClick={retry} disabled={loading}><MessageIcon name="regenerate" />Try again</button></div>}
      {deleteMessage && <Modal title="Delete from this message?" onClose={() => { if (!deleting) setDeleteMessage(null); }}><p className="text-sm text-muted">This message and all later replies will be deleted.</p><div className="flex gap-3 mt-6"><button type="button" className="deck-button" disabled={deleting} onClick={() => setDeleteMessage(null)}>Keep messages</button><button type="button" className="deck-button danger" disabled={deleting} onClick={truncate}>{deleting ? 'Deleting…' : 'Delete'}</button></div></Modal>}
      <div
        className="corner-panel border border-edge bg-panel focus-within:border-accent/60 transition-colors"
        onDrop={onDrop}
        onDragOver={onDragOver}
      >
        <div className="flex items-center justify-between px-4 py-2 border-b border-edge">
          <span className="font-mono text-[10px] tracking-widest text-muted/80 uppercase">prompt</span>
        </div>
        <textarea value={prompt} disabled={submitting} onChange={(e) => setPrompt(e.target.value)} onKeyDown={onKeyDown} onPaste={onTextareaPaste}
          placeholder={conversationId ? 'Follow up…' : 'Ask anything…'} rows={3}
          className="w-full bg-transparent resize-y outline-none px-4 pt-3 pb-1 text-[0.95rem] placeholder:text-muted/70 min-h-[4.5rem]" />
        {images.length > 0 && <div className="flex flex-wrap gap-2.5 px-4 pt-3 pb-1">{images.map((im, index) => <ImageThumb key={im.id} image={im} images={images} index={index} onRemove={removeImage} />)}</div>}

        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 border-t border-edge font-mono text-[11px] text-muted">
          <ModelSelect models={models} value={model} onChange={setModel} />


          <AttachmentPicker images={images} limits={limits} disabled={currentModel?.supports_images === false} onClick={() => fileInputRef.current?.click()} />
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
              {(submitting || busy) ? <span className="spinner" /> : null}
              {(submitting || busy) ? 'thinking…' : conversationId ? 'send' : 'brief'}
            </button>
          </div>
        </div>
      </div>

      {conversationId && <p className="font-mono text-[10px] text-muted flex items-center gap-2"><span className="text-good"><MessageIcon name="check" /></span>Saved automatically</p>}
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

      {(submitting || busy) && (
        <div className="text-sm text-muted flex items-center gap-2 px-1">
          <span className="spinner" />
          <span>{conversation?.job?.status === 'queued' ? 'Queued…' : 'Generating…'}</span>
        </div>
      )}

      {error && (
        <div className="bg-red-950/40 border border-bad/40 text-red-300 text-sm rounded-xl px-4 py-3">
          {error}
          {requestRef.current && !submitting && !busy && <button type="button" className="deck-button gap-2 mt-3" disabled={!!editing || !!regenerate} onClick={send}><MessageIcon name="regenerate" />Try again</button>}
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

function Main({ onLogout }) {
  const [tab, setTab] = useState('ask');
  const [openRequest, setOpenRequest] = useState(null);
  const workspaceActions = useRef(null);
  const [workspaceReady, setWorkspaceReady] = useState(false);
  const openConversation = (id) => { setOpenRequest({ id, nonce: crypto.randomUUID() }); setTab('ask'); };

  const [navOpen, setNavOpen] = useState(false);
  const [models, setModels] = useState([]);
  const [model, setModel] = useState('');
  const [defaultModel, setDefaultModel] = useState('');
  const [rate, setRate] = useState(1);
  const [imageLimits, setImageLimits] = useState(null);
  const [settings, setSettings] = useState(loadSettings);

  useEffect(() => {
    api('/config').then((r) => (r.ok ? r.json() : null)).then((d) => {
      if (!d) return;
      setModels(d.models);
      setModel(d.default);
      setDefaultModel(d.default);
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
      onClick={() => { window.dispatchEvent(new Event('briefai:before-navigation')); setTab(id); setNavOpen(false); }}
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
        <div className="max-w-6xl mx-auto px-5 h-14 flex items-center gap-2">
          <div className="font-mono font-bold text-[15px]">
            brief_ai<span className="text-accent glow">$</span>
          </div>
          <nav className="hidden sm:flex items-center gap-5 ml-8">
            {tabBtn('ask', 'ask')}
            {tabBtn('history', 'history')}
            {tabBtn('shared', 'shared')}
            {tabBtn('settings', 'settings')}
          </nav>
          <button type="button" className="header-new-conversation" title="New conversation" aria-label="New conversation" disabled={!workspaceReady} onClick={() => { setTab('ask'); workspaceActions.current?.newConversation(); }}>+</button>
          <button type="button" className="header-close-conversation" title="Close current conversation view" aria-label="Close conversation" disabled={!workspaceReady} onClick={() => { setTab('ask'); workspaceActions.current?.close(); }}>×</button>
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

      <main className="max-w-6xl mx-auto px-5 py-6">
        {models.length > 0 && (
          <div className={tab === 'ask' ? 'flex gap-8 items-start' : 'hidden'}>
            <div className="flex-1 min-w-0">
              <ConversationWorkspace models={models} defaultModel={defaultModel || models[0].id} rate={rate}
                imageLimits={imageLimits} settings={settings} onUnauth={unauth} visible={tab === 'ask'}
                openRequest={openRequest} actionsRef={workspaceActions} onReady={setWorkspaceReady} onModelChange={setModel} />
            </div>
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

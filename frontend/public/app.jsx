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

function fmtCost(usd) {
  if (usd < 0.01) return '$' + usd.toFixed(4);
  return '$' + usd.toFixed(3);
}

function fmtTime(iso) {
  try {
    return new Date(iso).toLocaleString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  } catch (e) {
    return iso;
  }
}

function Markdown({ text }) {
  const ref = useRef(null);
  const html = useMemo(() => {
    marked.setOptions({ gfm: true, breaks: true });
    return DOMPurify.sanitize(marked.parse(text || ''));
  }, [text]);
  useEffect(() => {
    if (!ref.current) return;
    ref.current.querySelectorAll('pre code').forEach((el) => {
      try { hljs.highlightElement(el); } catch (e) { /* noop */ }
    });
  }, [html]);
  return <div ref={ref} className="md" dangerouslySetInnerHTML={{ __html: html }} />;
}

function Meta({ result, onCopy, copied }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted border-b border-edge pb-3 mb-4">
      <span className="text-accent2 font-medium">{result.model_label}</span>
      <span>{fmtDuration(result.duration_ms)}</span>
      <span>{result.input_tokens.toLocaleString()} in / {result.output_tokens.toLocaleString()} out</span>
      <span>{fmtCost(result.cost_usd)}</span>
      {onCopy && (
        <button
          onClick={onCopy}
          className="ml-auto text-muted hover:text-white transition-colors"
        >
          {copied ? 'Copied' : 'Copy'}
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
      const res = await api('/login', { method: 'POST', body: JSON.stringify({ pin: code }) });
      if (res.ok) { onSuccess(); return; }
      const data = await res.json().catch(() => ({}));
      if (res.status === 429) {
        setCooldown(data.retry_after || 3);
        setError('Too many attempts — wait a moment.');
      } else {
        setError('Incorrect PIN.');
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
          <h1 className="text-2xl font-bold tracking-tight">
            Brief<span className="text-accent">AI</span>
          </h1>
          <p className="text-muted text-sm mt-1">Enter your PIN to continue</p>
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

function AskView({ models, model, setModel, onUnauth }) {
  const [prompt, setPrompt] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  const [elapsed, setElapsed] = useState(0);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!loading) return;
    const start = Date.now();
    setElapsed(0);
    const t = setInterval(() => setElapsed(Date.now() - start), 100);
    return () => clearInterval(t);
  }, [loading]);

  const send = async () => {
    if (!prompt.trim() || loading) return;
    setLoading(true);
    setResult(null);
    setError('');
    setCopied(false);
    try {
      const res = await api('/generate', {
        method: 'POST',
        body: JSON.stringify({ model, prompt }),
      });
      if (res.status === 401) { onUnauth(); return; }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(data.detail || data.error || 'Request failed.');
      } else {
        setResult(data);
      }
    } catch (e) {
      setError('Network error.');
    } finally {
      setLoading(false);
    }
  };

  const onKeyDown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      send();
    }
  };

  const copy = () => {
    if (!result) return;
    navigator.clipboard.writeText(result.answer).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div className="space-y-4">
      <div className="bg-panel border border-edge rounded-2xl p-1.5 focus-within:border-accent/60 transition-colors">
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Ask anything…"
          rows={4}
          className="w-full bg-transparent resize-y outline-none px-3.5 py-3 text-[0.95rem] placeholder:text-muted/70 min-h-[7rem]"
        />
        <div className="flex items-center justify-between px-2.5 pb-1.5 pt-1">
          <select
            value={model}
            onChange={(e) => setModel(e.target.value)}
            className="bg-panel2 border border-edge rounded-lg text-sm px-2.5 py-1.5 outline-none hover:border-accent/50 transition cursor-pointer"
          >
            {models.map((m) => (
              <option key={m.id} value={m.id}>{m.label}</option>
            ))}
          </select>

          <button
            onClick={send}
            disabled={loading || !prompt.trim()}
            className="flex items-center gap-2 bg-accent hover:bg-accent2 disabled:opacity-40 disabled:hover:bg-accent text-white text-sm font-medium px-4 py-1.5 rounded-lg transition-colors"
          >
            {loading ? <span className="spinner" /> : null}
            {loading ? 'Thinking' : 'Send'}
          </button>
        </div>
      </div>

      <p className="text-xs text-muted/70 px-1">
        Press <kbd className="font-mono">{navigator.platform.includes('Mac') ? '⌘' : 'Ctrl'}</kbd>
        {' '}+ <kbd className="font-mono">↵</kbd> to send
      </p>

      {loading && (
        <div className="text-sm text-muted flex items-center gap-2 px-1">
          <span className="spinner" />
          <span>Generating… {(elapsed / 1000).toFixed(1)}s</span>
        </div>
      )}

      {error && (
        <div className="bg-red-950/40 border border-red-900/60 text-red-300 text-sm rounded-xl px-4 py-3">
          {error}
        </div>
      )}

      {result && (
        <div className="bg-panel border border-edge rounded-2xl p-5 fade-in">
          <Meta result={result} onCopy={copy} copied={copied} />
          <Markdown text={result.answer} />
        </div>
      )}
    </div>
  );
}

function HistoryView({ onUnauth }) {
  const [items, setItems] = useState(null);
  const [detail, setDetail] = useState(null);

  const load = useCallback(async () => {
    try {
      const res = await api('/history');
      if (res.status === 401) { onUnauth(); return; }
      const data = await res.json();
      setItems(data.items || []);
    } catch (e) {
      setItems([]);
    }
  }, [onUnauth]);

  useEffect(() => { load(); }, [load]);

  const open = async (id) => {
    try {
      const res = await api('/history/' + id);
      if (res.status === 401) { onUnauth(); return; }
      if (res.ok) setDetail(await res.json());
    } catch (e) { /* noop */ }
  };

  if (items === null) {
    return <div className="text-muted text-sm px-1">Loading…</div>;
  }
  if (items.length === 0) {
    return <div className="text-muted text-sm px-1">No prompts yet.</div>;
  }

  return (
    <div className="space-y-2">
      {items.map((it) => (
        <button
          key={it.id}
          onClick={() => open(it.id)}
          className="w-full text-left bg-panel border border-edge hover:border-accent/50 rounded-xl px-4 py-3 transition-colors"
        >
          <div className="flex items-center gap-3 text-xs text-muted mb-1.5">
            <span className="text-accent2 font-medium">{it.model_label}</span>
            <span>{fmtTime(it.created_at)}</span>
            <span className="ml-auto">{fmtCost(it.cost_usd)}</span>
            <span>{fmtDuration(it.duration_ms)}</span>
          </div>
          <div className="text-sm text-[#dcdce2] line-clamp-2">{it.prompt_preview}</div>
        </button>
      ))}

      {detail && (
        <div
          className="fixed inset-0 z-20 bg-black/70 backdrop-blur-sm grid place-items-center p-4"
          onClick={() => setDetail(null)}
        >
          <div
            className="bg-panel border border-edge rounded-2xl w-full max-w-2xl max-h-[85vh] overflow-y-auto p-5 fade-in"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-4">
              <span className="text-xs text-muted">{fmtTime(detail.created_at)}</span>
              <button
                onClick={() => setDetail(null)}
                className="text-muted hover:text-white text-sm"
              >
                Close
              </button>
            </div>
            <div className="text-xs uppercase tracking-wide text-muted mb-1.5">Prompt</div>
            <div className="bg-panel2 border border-edge rounded-xl px-4 py-3 text-sm whitespace-pre-wrap mb-5">
              {detail.prompt}
            </div>
            <div className="text-xs uppercase tracking-wide text-muted mb-2">Answer</div>
            <Meta result={detail} />
            <Markdown text={detail.answer} />
          </div>
        </div>
      )}
    </div>
  );
}

function Main({ onLogout }) {
  const [tab, setTab] = useState('ask');
  const [models, setModels] = useState([]);
  const [model, setModel] = useState('');

  useEffect(() => {
    api('/config').then((r) => (r.ok ? r.json() : null)).then((d) => {
      if (!d) return;
      setModels(d.models);
      setModel(d.default);
    });
  }, []);

  const logout = async () => {
    try { await api('/logout', { method: 'POST' }); } catch (e) { /* noop */ }
    onLogout();
  };

  const unauth = () => onLogout();

  const tabBtn = (id, label) => (
    <button
      onClick={() => setTab(id)}
      className={
        'px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ' +
        (tab === id ? 'bg-panel2 text-white' : 'text-muted hover:text-white')
      }
    >
      {label}
    </button>
  );

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-10 backdrop-blur bg-ink/80 border-b border-edge">
        <div className="max-w-2xl mx-auto px-5 h-14 flex items-center gap-3">
          <div className="font-bold tracking-tight">
            Brief<span className="text-accent">AI</span>
          </div>
          <nav className="flex items-center gap-1 ml-2">
            {tabBtn('ask', 'Ask')}
            {tabBtn('history', 'History')}
          </nav>
          <button
            onClick={logout}
            className="ml-auto text-sm text-muted hover:text-white transition-colors"
          >
            Log out
          </button>
        </div>
      </header>

      <main className="max-w-2xl mx-auto px-5 py-6">
        {tab === 'ask' && models.length > 0 && (
          <AskView models={models} model={model} setModel={setModel} onUnauth={unauth} />
        )}
        {tab === 'history' && <HistoryView onUnauth={unauth} />}
      </main>
    </div>
  );
}

function App() {
  const [ready, setReady] = useState(false);
  const [authed, setAuthed] = useState(false);

  useEffect(() => {
    api('/session')
      .then((r) => r.json())
      .then((d) => { setAuthed(!!d.authenticated); setReady(true); })
      .catch(() => setReady(true));
  }, []);

  if (!ready) {
    return <div className="min-h-screen grid place-items-center"><div className="spinner" /></div>;
  }

  return authed
    ? <Main onLogout={() => setAuthed(false)} />
    : <Login onSuccess={() => setAuthed(true)} />;
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);

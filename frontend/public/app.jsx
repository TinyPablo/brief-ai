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
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
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

function composePrompt(prompt, settings, brief) {
  const ctx = buildContext(settings);
  const prefix = brief ? 'very brief: ' : '';
  return (ctx ? ctx + '\n\n' : '') + prefix + prompt;
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
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted border-b border-edge pb-3 mb-4">
      <span className="text-accent2 font-medium">{result.model_label}</span>
      {result.reasoning && <span>{result.reasoning}</span>}
      <span>{fmtDuration(result.duration_ms)}</span>
      <span>{result.input_tokens.toLocaleString()} in / {result.output_tokens.toLocaleString()} out</span>
      <span>{zl(result.cost_usd * rate)}</span>
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

function groupByProvider(models) {
  const groups = [];
  models.forEach((m) => {
    let g = groups.find((x) => x.label === m.provider_label);
    if (!g) { g = { label: m.provider_label, items: [] }; groups.push(g); }
    g.items.push(m);
  });
  return groups;
}

function AskView({ models, model, setModel, effort, setEffort, thinking, setThinking, rate, maxTokens, settings, onUnauth }) {
  const groups = useMemo(() => groupByProvider(models), [models]);
  const currentModel = models.find((m) => m.id === model) || null;
  const controls = currentModel ? (currentModel.controls || []) : [];
  const fixed = currentModel ? (currentModel.fixed || []) : [];
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

  const estimate = useMemo(() => {
    if (!currentModel || !prompt.trim()) return null;
    const composed = composePrompt(prompt, settings, false);
    const inTok = Math.ceil(composed.length / 4);
    const outTok = Math.min(Math.round(inTok * 1.5), maxTokens);
    const usd = inTok / 1e6 * currentModel.input + outTok / 1e6 * currentModel.output;
    return usd * rate;
  }, [prompt, currentModel, settings, maxTokens, rate]);

  const doSend = async (brief) => {
    if (!prompt.trim() || loading) return;
    const text = composePrompt(prompt, settings, brief);
    setLoading(true);
    setResult(null);
    setError('');
    setCopied(false);
    try {
      const res = await api('/generate', {
        method: 'POST',
        body: JSON.stringify({ model, prompt: text, effort, thinking }),
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

  const send = () => doSend(false);
  const sendBrief = () => doSend(true);

  const clearAll = () => {
    setPrompt('');
    setResult(null);
    setError('');
    setCopied(false);
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
        <div className="flex flex-wrap items-center justify-between gap-2 px-2.5 pb-1.5 pt-1">
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="bg-panel2 border border-edge rounded-lg text-sm px-2.5 py-1.5 outline-none hover:border-accent/50 transition cursor-pointer"
            >
              {groups.map((g) => (
                <optgroup key={g.label} label={g.label}>
                  {g.items.map((m) => (
                    <option key={m.id} value={m.id}>{m.label} · ~{zl(m.est_pln)}</option>
                  ))}
                </optgroup>
              ))}
            </select>

            {controls.map((c) => {
              const value = c.id === 'effort' ? effort : thinking;
              const setValue = c.id === 'effort' ? setEffort : setThinking;
              return (
                <label key={c.id} className="flex items-center gap-1.5 text-xs text-muted">
                  {c.label}
                  <select
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    className="bg-panel2 border border-edge rounded-lg text-sm px-2 py-1.5 outline-none hover:border-accent/50 transition cursor-pointer"
                  >
                    {c.options.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                </label>
              );
            })}

            {fixed.map((f, i) => (
              <span key={i} className="text-xs text-muted/70 border border-edge rounded-lg px-2 py-1.5">
                {f.label}: {f.value}
              </span>
            ))}

            {controls.length === 0 && fixed.length === 0 && (
              <span className="text-xs text-muted/70 border border-edge rounded-lg px-2 py-1.5">Default</span>
            )}
          </div>

          <div className="flex items-center gap-2">
            <button
              onClick={clearAll}
              disabled={loading || (!prompt && !result && !error)}
              className="text-sm text-muted hover:text-white disabled:opacity-40 px-3 py-1.5 rounded-lg transition-colors"
            >
              Clear
            </button>
            <button
              onClick={sendBrief}
              disabled={loading || !prompt.trim()}
              title="Ask for a very brief answer"
              className="text-sm font-medium text-accent2 border border-accent/60 hover:bg-accent/10 disabled:opacity-40 disabled:hover:bg-transparent px-3 py-1.5 rounded-lg transition-colors"
            >
              Brief
            </button>
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
      </div>

      <div className="flex items-center justify-between gap-2 px-1 text-xs text-muted/70">
        <span>
          Press <kbd className="font-mono">{navigator.platform.includes('Mac') ? '⌘' : 'Ctrl'}</kbd>
          {' '}+ <kbd className="font-mono">↵</kbd> to send
        </span>
        {estimate != null && <span>est. ~{zl(estimate)}</span>}
      </div>

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
          <Meta result={result} rate={rate} onCopy={copy} copied={copied} />
          <Markdown text={result.answer} />
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
    + ' – ' + e.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
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

function HistoryView({ rate, onUnauth }) {
  const [items, setItems] = useState([]);
  const [q, setQ] = useState('');
  const [loading, setLoading] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [openState, setOpenState] = useState({});
  const [detail, setDetail] = useState(null);

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
  }, [q, onUnauth]);

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

  const isOpen = (key, type) => {
    if (openState[key] !== undefined) return openState[key];
    return type === 'week' ? key === curWeek : key === curDay;
  };
  const toggle = (key, type) => setOpenState((s) => ({ ...s, [key]: !isOpen(key, type) }));

  const open = async (id) => {
    try {
      const res = await api('/history/' + id);
      if (res.status === 401) { onUnauth(); return; }
      if (res.ok) setDetail(await res.json());
    } catch (e) { /* noop */ }
  };

  const del = async (id, e) => {
    e.stopPropagation();
    if (!window.confirm('Delete this conversation?')) return;
    try {
      const res = await api('/history/' + id, { method: 'DELETE' });
      if (res.status === 401) { onUnauth(); return; }
      if (res.ok) {
        setItems((prev) => prev.filter((x) => x.id !== id));
        if (detail && detail.id === id) setDetail(null);
      }
    } catch (e2) { /* noop */ }
  };

  const groups = useMemo(() => groupHistory(items), [items]);

  return (
    <div className="space-y-3">
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
          className="w-full bg-panel border border-edge rounded-xl text-sm pl-10 pr-4 py-2.5 outline-none focus:border-accent/60 transition-colors"
        />
      </div>

      {loaded && items.length === 0 && (
        <div className="text-muted text-sm px-1">{q.trim() ? 'No matches.' : 'No prompts yet.'}</div>
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
                    <div
                      key={it.id}
                      className="group flex items-start gap-2 bg-panel border border-edge hover:border-accent/50 rounded-xl px-4 py-3 transition-colors"
                    >
                      <button onClick={() => open(it.id)} className="flex-1 min-w-0 text-left">
                        <div className="flex items-center gap-3 text-xs text-muted mb-1.5">
                          <span className="text-accent2 font-medium">{it.model_label}</span>
                          {it.reasoning && <span>{it.reasoning}</span>}
                          <span>{fmtTime(it.created_at)}</span>
                          <span className="ml-auto">{zl(it.cost_usd * rate)}</span>
                          <span>{fmtDuration(it.duration_ms)}</span>
                        </div>
                        <div className="text-sm text-[#dcdce2] line-clamp-2">{it.prompt_preview}</div>
                      </button>
                      <button
                        onClick={(e) => del(it.id, e)}
                        title="Delete"
                        className="shrink-0 text-muted hover:text-red-400 text-sm opacity-0 group-hover:opacity-100 transition-opacity"
                      >
                        ✕
                      </button>
                    </div>
                  ))}
                </div>
              );
            })}
          </div>
        );
      })}

      {loading && <div className="flex justify-center py-3"><span className="spinner" /></div>}
      <div ref={sentinelRef} className="h-1" />

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
              <div className="flex items-center gap-4">
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
            <div className="text-xs uppercase tracking-wide text-muted mb-1.5">Prompt</div>
            <div className="bg-panel2 border border-edge rounded-xl px-4 py-3 text-sm whitespace-pre-wrap mb-5">
              {detail.prompt}
            </div>
            <div className="text-xs uppercase tracking-wide text-muted mb-2">Answer</div>
            <Meta result={detail} rate={rate} />
            <Markdown text={detail.answer} />
          </div>
        </div>
      )}
    </div>
  );
}

function SettingsView({ settings, setSettings }) {
  const upd = (patch) => setSettings((s) => ({ ...s, ...patch }));
  return (
    <div className="bg-panel border border-edge rounded-2xl p-5 space-y-5 fade-in">
      <p className="text-sm text-muted">
        Context is attached above every prompt. Toggle what gets sent to the model.
      </p>

      <label className="flex items-center gap-3 cursor-pointer">
        <input
          type="checkbox"
          checked={settings.nowOn}
          onChange={(e) => upd({ nowOn: e.target.checked })}
          className="w-4 h-4 accent-accent"
        />
        <span className="text-sm">Date &amp; time (now)</span>
      </label>

      <div className="space-y-2">
        <label className="flex items-center gap-3 cursor-pointer">
          <input
            type="checkbox"
            checked={settings.locOn}
            onChange={(e) => upd({ locOn: e.target.checked })}
            className="w-4 h-4 accent-accent"
          />
          <span className="text-sm">Location</span>
        </label>
        <input
          type="text"
          value={settings.locText}
          onChange={(e) => upd({ locText: e.target.value })}
          disabled={!settings.locOn}
          placeholder="e.g. Bielsko-Biała"
          className="w-full bg-panel2 border border-edge rounded-lg text-sm px-3 py-2 outline-none focus:border-accent/60 disabled:opacity-40"
        />
      </div>

      <div className="space-y-2">
        <label className="flex items-center gap-3 cursor-pointer">
          <input
            type="checkbox"
            checked={settings.userOn}
            onChange={(e) => upd({ userOn: e.target.checked })}
            className="w-4 h-4 accent-accent"
          />
          <span className="text-sm">User data</span>
        </label>
        <textarea
          value={settings.userText}
          onChange={(e) => upd({ userText: e.target.value })}
          disabled={!settings.userOn}
          rows={3}
          placeholder="e.g. 20yo, 181cm, 62kg, bulking ~3 months"
          className="w-full bg-panel2 border border-edge rounded-lg text-sm px-3 py-2 outline-none resize-y focus:border-accent/60 disabled:opacity-40"
        />
      </div>

      <p className="text-xs text-muted/70">Saved automatically in this browser.</p>
    </div>
  );
}

function Main({ onLogout }) {
  const [tab, setTab] = useState('ask');
  const [models, setModels] = useState([]);
  const [model, setModel] = useState('');
  const [rate, setRate] = useState(1);
  const [maxTokens, setMaxTokens] = useState(4096);
  const [effort, setEffort] = useState('low');
  const [thinking, setThinking] = useState('off');
  const [settings, setSettings] = useState(loadSettings);

  useEffect(() => {
    api('/config').then((r) => (r.ok ? r.json() : null)).then((d) => {
      if (!d) return;
      setModels(d.models);
      setModel(d.default);
      setRate(d.usd_pln || 1);
      setMaxTokens(d.max_tokens || 4096);
    });
  }, []);

  // Reset effort/thinking to the selected model's control defaults (always lowest).
  useEffect(() => {
    const m = models.find((x) => x.id === model);
    if (!m) return;
    const ef = (m.controls || []).find((c) => c.id === 'effort');
    const th = (m.controls || []).find((c) => c.id === 'thinking');
    setEffort(ef ? ef.default : 'low');
    setThinking(th ? th.default : 'off');
  }, [model, models]);

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
            {tabBtn('settings', 'Settings')}
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
          <AskView
            models={models}
            model={model}
            setModel={setModel}
            effort={effort}
            setEffort={setEffort}
            thinking={thinking}
            setThinking={setThinking}
            rate={rate}
            maxTokens={maxTokens}
            settings={settings}
            onUnauth={unauth}
          />
        )}
        {tab === 'history' && <HistoryView rate={rate} onUnauth={unauth} />}
        {tab === 'settings' && <SettingsView settings={settings} setSettings={setSettings} />}
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

(function () {
  'use strict';
  let owner = null;
  const clean = (text) => text.replace(/<(?:end|fin)>/g, '');
  function reduce(state, message) {
    if (!Array.isArray(message.tokens) || !message.tokens.length) return state;
    let partial = '';
    for (const token of message.tokens) {
      if (typeof token.text !== 'string') continue;
      if (token.is_final) state.stable += clean(token.text);
      else partial += clean(token.text);
    }
    state.partial = partial;
    return state;
  }
  function supported() {
    return !!(window.isSecureContext && navigator.mediaDevices?.getUserMedia && window.AudioContext && window.AudioWorkletNode);
  }
  const errors = {
    dictation_unavailable: 'Dictation is not configured.',
    dictation_balance_exhausted: 'Soniox balance exhausted. Top up your account and resume.',
    organization_balance_exhausted: 'Soniox balance exhausted. Top up your account and resume.',
    NotAllowedError: 'Microphone access was denied. Allow it in your browser and resume.',
    NotFoundError: 'No microphone found. Connect one and resume.',
    NotReadableError: 'The microphone is unavailable. Check other apps and resume.',
    unauthorized: 'Your session expired. Sign in again.',
    unsupported: 'Dictation requires HTTPS and a browser with microphone support.',
    occupied: 'Stop the other microphone first.',
  };
  class Session {
    constructor({ onText, onState, onUnauth }) {
      this.onText = onText;
      this.onState = onState;
      this.onUnauth = onUnauth;
      this.state = 'idle';
    }
    notify(state, error = '') {
      this.state = state;
      this.onState?.({ state, error });
    }
    publish() {
      const speech = this.text.stable + this.text.partial;
      const separator = this.base && speech && !/\s$/.test(this.base) && !/^\s/.test(speech) ? ' ' : '';
      const value = this.base + separator + speech;
      if (value.length > 100000) { this.fail('length'); return; }
      this.onText?.(value);
    }
    async start(base) {
      if (['starting', 'recording', 'finishing'].includes(this.state)) return;
      if (!supported()) { this.notify('error', errors.unsupported); return; }
      if (owner && owner !== this) { this.notify('error', errors.occupied); return; }
      owner = this;
      this.base = base;
      this.text = { stable: '', partial: '' };
      this.closed = false;
      this.notify('starting');
      this.abort = new AbortController();
      this.timer = setTimeout(() => this.fail('timeout'), 15000);
      try {
        // Start permission and audio activation within the user's gesture (Safari).
        this.context = new AudioContext();
        const resumed = this.context.resume();
        resumed.catch(() => {});
        const requested = navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false });
        const stream = await requested;
        if (this.closed) { stream.getTracks().forEach((track) => track.stop()); return; }
        this.stream = stream;
        await resumed;
        if (this.closed) return;
        if (this.context.sampleRate < 16000) throw new Error('sample_rate');
        stream.getAudioTracks().forEach((track) => {
          track.onended = () => this.fail('interrupted');
          track.onmute = () => this.fail('interrupted');
        });
        const response = await fetch('/api/dictation/session', {
          method: 'POST', credentials: 'same-origin', headers: { 'X-Brief-Dictation': '1' }, signal: this.abort.signal,
        });
        if (this.closed) return;
        if (response.status === 401) { this.onUnauth?.(); throw new Error('unauthorized'); }
        const credential = await response.json();
        if (this.closed) return;
        if (!response.ok || !credential.api_key) throw new Error(credential.error || 'connection');
        await this.context.audioWorklet.addModule('/dictation-worklet.js');
        if (this.closed) return;
        this.node = new AudioWorkletNode(this.context, 'dictation-capture');
        this.node.port.onmessage = ({ data }) => {
          if (this.closed) return;
          if (data.type === 'audio' && this.socket?.readyState === WebSocket.OPEN) {
            if (this.socket.bufferedAmount > 1024 * 1024) { this.fail('connection'); return; }
            this.socket.send(data.bytes);
          }
          if (data.type === 'flushed' && this.state === 'finishing') {
            if (this.socket?.readyState === WebSocket.OPEN) this.socket.send('');
            else this.fail('connection');
          }
        };
        const socket = this.socket = new WebSocket('wss://stt-rt.soniox.com/transcribe-websocket', ['soniox-api-key', credential.api_key]);
        socket.onopen = () => {
          if (this.closed) return;
          try {
            socket.send(JSON.stringify({
              model: 'stt-rt-v5', audio_format: 'pcm_s16le', sample_rate: 16000, num_channels: 1,
              language_hints: ['pl', 'en'], enable_endpoint_detection: true, enable_language_identification: true,
              context: {
                general: [{ key: 'domain', value: 'Natural Polish and English dictation about software development, strength training and everyday topics' }],
                terms: ['Brief AI', 'React', 'useEffect', 'Docker', 'PostgreSQL', 'GitHub', 'pull request', 'API', 'MCP', 'TypeScript', 'scapular push-ups', '1RM', 'RPE', 'RIR', 'bench press', 'deadlift'],
              },
            }));
            this.source = this.context.createMediaStreamSource(this.stream);
            this.sink = this.context.createGain();
            this.sink.gain.value = 0;
            this.source.connect(this.node);
            this.node.connect(this.sink);
            this.sink.connect(this.context.destination);
            clearTimeout(this.timer);
            this.context.onstatechange = () => { if (this.state === 'recording' && this.context?.state !== 'running') this.fail('interrupted'); };
            this.notify('recording');
          } catch { this.fail('interrupted'); }
        };
        socket.onmessage = ({ data }) => {
          if (this.closed) return;
          try {
            const message = JSON.parse(data);
            if (message.error_code || message.error_type) { this.fail(message.error_type || 'provider'); return; }
            reduce(this.text, message);
            this.publish();
            if (message.finished) {
              if (this.state !== 'finishing') { this.fail('interrupted'); return; }
              const provisional = !!this.text.partial;
              this.cleanup();
              this.notify(provisional ? 'error' : 'idle', provisional ? 'Text preserved; check the last words before continuing.' : '');
            }
          } catch { this.fail('provider'); }
        };
        socket.onerror = () => this.fail('connection');
        socket.onclose = () => { if (!this.closed) this.fail('connection'); };
      } catch (error) { if (!this.closed) this.fail(error.name in errors ? error.name : error.message); }
    }
    stop() {
      if (this.state === 'starting') { this.cleanup(); this.notify('idle'); return; }
      if (this.state !== 'recording') return;
      this.notify('finishing');
      this.stopTracks();
      this.source?.disconnect();
      this.node.port.postMessage({ type: 'flush' });
      this.timer = setTimeout(() => this.fail('finalization'), 8000);
    }
    stopTracks() {
      this.stream?.getTracks().forEach((track) => { track.onended = null; track.onmute = null; track.stop(); });
    }
    fail(reason) {
      if (this.closed) return;
      this.cleanup();
      const message = reason === 'length' ? 'Prompt limit reached. Text preserved.' : errors[reason] || 'Dictation interrupted. Visible text preserved; resume to continue.';
      this.notify('error', message);
    }
    cleanup() {
      this.closed = true;
      clearTimeout(this.timer);
      this.abort?.abort();
      this.stopTracks();
      this.source?.disconnect();
      this.node?.disconnect();
      this.sink?.disconnect();
      if (this.context) { this.context.onstatechange = null; this.context.close().catch(() => {}); }
      if (this.socket) { this.socket.onopen = this.socket.onmessage = this.socket.onclose = this.socket.onerror = null; this.socket.close(); }
      if (owner === this) owner = null;
    }
    dispose() { this.onText = this.onState = this.onUnauth = null; this.cleanup(); }
  }
  window.BriefDictation = { Session, supported, reduce };
})();

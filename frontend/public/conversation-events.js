/* One authenticated update connection per page; snapshots remain authoritative. */
(() => {
  let connected = false;
  const update = (id = null) => window.dispatchEvent(new CustomEvent('briefai:conversation-update', { detail: { id } }));
  window.BriefConversationEvents = {
    connected: () => connected,
    start() {
      if (!window.EventSource) return () => {};
      const source = new EventSource('/api/conversation-events');
      const readiness = (value) => { connected = value; update(); };
      source.addEventListener('ready', () => readiness(true));
      source.addEventListener('error', () => readiness(false));
      source.addEventListener('unavailable', () => readiness(false));
      source.addEventListener('changed', (event) => {
        try {
          const data = JSON.parse(event.data);
          if (typeof data.id === 'string') update(data.id);
        } catch { /* The next snapshot reconciliation recovers missed updates. */ }
      });
      const visible = () => { if (!document.hidden) update(); };
      window.addEventListener('pageshow', visible);
      document.addEventListener('visibilitychange', visible);
      return () => {
        source.close(); connected = false;
        window.removeEventListener('pageshow', visible);
        document.removeEventListener('visibilitychange', visible);
      };
    },
  };
})();

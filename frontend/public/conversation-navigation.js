(() => {
  function blocked(target) {
    return !!document.querySelector('dialog[open]') ||
      !!target?.closest('textarea, input, select, button, a, pre, code, [contenteditable=true]') ||
      !!window.getSelection()?.toString();
  }
  function gestures(element, enabled, navigate) {
    let total = 0, direction = 0, locked = false, release, last = 0, touch = null;
    const wheel = (event) => {
      if (!enabled() || event.ctrlKey || blocked(event.target) ||
          Math.abs(event.deltaX) < 1 || Math.abs(event.deltaX) <= Math.abs(event.deltaY) * 1.5) return;
      if (event.cancelable) event.preventDefault();
      clearTimeout(release);
      release = setTimeout(() => { locked = false; total = 0; direction = 0; }, 200);
      if (locked) return;
      const sign = Math.sign(event.deltaX);
      if (performance.now() - last > 200 || sign !== direction) total = 0;
      last = performance.now(); direction = sign;
      total += event.deltaX * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? innerWidth : 1);
      if (Math.abs(total) >= 70) { locked = true; total = 0; navigate(sign); }
    };
    const start = (event) => {
      touch = null;
      if (!enabled() || event.touches.length !== 1 || blocked(event.target)) return;
      const point = event.touches[0];
      if (point.clientX < 24 || point.clientX > innerWidth - 24) return;
      touch = { x: point.clientX, y: point.clientY, horizontal: false };
    };
    const move = (event) => {
      if (!touch || event.touches.length !== 1) { touch = null; return; }
      const dx = event.touches[0].clientX - touch.x, dy = event.touches[0].clientY - touch.y;
      if (Math.abs(dy) > 14 && Math.abs(dy) > Math.abs(dx)) { touch = null; return; }
      if (Math.abs(dx) > 14 && Math.abs(dx) > Math.abs(dy) * 1.5) touch.horizontal = true;
      if (touch.horizontal && event.cancelable) event.preventDefault();
    };
    const end = (event) => {
      if (touch && enabled() && event.changedTouches.length) {
        const dx = event.changedTouches[0].clientX - touch.x;
        const dy = event.changedTouches[0].clientY - touch.y;
        if (Math.abs(dx) >= 70 && Math.abs(dx) > Math.abs(dy) * 1.5) navigate(dx < 0 ? 1 : -1);
      }
      touch = null;
    };
    const cancel = () => { touch = null; };
    window.addEventListener('wheel', wheel, { passive: false });
    element.addEventListener('touchstart', start, { passive: true });
    element.addEventListener('touchmove', move, { passive: false });
    element.addEventListener('touchend', end, { passive: true });
    element.addEventListener('touchcancel', cancel);
    return () => {
      clearTimeout(release);
      window.removeEventListener('wheel', wheel);
      element.removeEventListener('touchstart', start);
      element.removeEventListener('touchmove', move);
      element.removeEventListener('touchend', end);
      element.removeEventListener('touchcancel', cancel);
    };
  }

  class Transitions {
    constructor(layer) { this.layer = layer; this.animations = []; this.version = 0; }
    cancel() {
      this.version++;
      this.animations.forEach((animation) => animation.cancel());
      this.animations = [];
      this.layer.replaceChildren();
      delete this.layer.dataset.direction;
      this.played = false;
    }
    capture(element, direction) {
      this.cancel();
      if (!element || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      const rect = element.getBoundingClientRect();
      const top = Math.max(document.querySelector('header')?.getBoundingClientRect().bottom || 0, rect.top);
      const layerRect = this.layer.getBoundingClientRect();
      const scale = layerRect.width / this.layer.clientWidth || 1;
      const frame = document.createElement('div');
      frame.className = 'conversation-outgoing';
      Object.assign(frame.style, { left: (rect.left - layerRect.left) / scale + 'px', top: (top - layerRect.top) / scale + 'px', width: rect.width / scale + 'px', height: Math.max(0, innerHeight - top) / scale + 'px' });
      const copy = element.cloneNode(true);
      copy.removeAttribute('data-conversation-view'); copy.removeAttribute('data-conversation-id');
      copy.querySelectorAll('[id]').forEach((node) => node.removeAttribute('id'));
      copy.inert = true;
      this.offsetY = (rect.top - top) / scale;
      copy.style.transform = `translateY(${this.offsetY}px)`;
      copy.style.background = '#0d0c0a';
      copy.style.minHeight = Math.max(0, innerHeight - top) / scale + 'px';
      frame.style.background = 'transparent';
      const originals = element.querySelectorAll('textarea, input, select');
      copy.querySelectorAll('textarea, input, select').forEach((node, index) => { node.value = originals[index].value; });
      frame.append(copy);
      this.layer.append(frame);
      this.layer.dataset.direction = direction > 0 ? 'forward' : 'backward';
      this.direction = direction;
    }
    play(element) {
      const frame = this.layer.firstElementChild;
      if (!frame || !element || this.played) return;
      this.played = true;
      const version = this.version;
      const width = frame.clientWidth;
      const options = { duration: 360, easing: 'cubic-bezier(.22,.68,.18,1)', fill: 'both' };
      this.animations = [
        frame.firstElementChild.animate([{ transform: `translate(0, ${this.offsetY}px)`, opacity: 1 }, { transform: `translate(${-this.direction * width}px, ${this.offsetY}px)`, opacity: 0 }], options),
        element.animate([{ transform: `translateX(${this.direction * width}px)`, opacity: 0 }, { transform: 'translateX(0)', opacity: 1 }], options),
      ];
      Promise.all(this.animations.map((animation) => animation.finished.catch(() => {}))).then(() => {
        if (version === this.version) this.cancel();
      });
    }
  }
  window.BriefNavigation = { gestures, Transitions };
})();

class DictationCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunk = new Int16Array(1600);
    this.length = 0;
    this.phase = 0;
    this.sum = 0;
    this.samples = 0;
    this.port.onmessage = (event) => {
      if (event.data.type === 'flush') {
        this.emit();
        this.port.postMessage({ type: 'flushed' });
      }
    };
  }
  emit() {
    if (!this.length) return;
    const bytes = new ArrayBuffer(this.length * 2);
    const view = new DataView(bytes);
    for (let i = 0; i < this.length; i++) view.setInt16(i * 2, this.chunk[i], true);
    this.port.postMessage({ type: 'audio', bytes }, [bytes]);
    this.length = 0;
  }
  process(inputs) {
    const channels = inputs[0];
    if (!channels?.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      let value = 0;
      for (const channel of channels) value += channel[i] / channels.length;
      this.sum += value;
      this.samples++;
      this.phase += 16000;
      if (this.phase >= sampleRate) {
        const mono = Math.max(-1, Math.min(1, this.sum / this.samples));
        this.chunk[this.length++] = Math.round(mono * (mono < 0 ? 32768 : 32767));
        this.phase -= sampleRate;
        this.sum = 0;
        this.samples = 0;
        if (this.length === this.chunk.length) this.emit();
      }
    }
    return true;
  }
}
registerProcessor('dictation-capture', DictationCapture);

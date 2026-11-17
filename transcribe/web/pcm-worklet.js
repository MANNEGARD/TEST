// AudioWorklet: tar emot ljud (mono, webbläsarens samplingsfrekvens), samplar ned till
// 16 kHz med ett enkelt lågpassfilter (medelvärde över fönstret) och skickar 16-bitars PCM
// i bitar om 100 ms till huvudtråden.
const TARGET = 16000;
const CHUNK = 1600; // 100 ms

class PcmDownsampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET;
    this.buf = new Float32Array(0);
    this.pos = 0;
    this.out = new Int16Array(CHUNK);
    this.outLen = 0;
    this.paused = false;
    this.port.onmessage = (e) => {
      if (e.data && e.data.type === "pause") this.paused = !!e.data.paused;
    };
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0 || this.paused) return true;
    const ch = input[0];
    if (!ch) return true;
    // Lägg till nya sampel efter de som inte förbrukats än.
    const merged = new Float32Array(this.buf.length + ch.length);
    merged.set(this.buf, 0);
    merged.set(ch, this.buf.length);
    const ratio = this.ratio;
    let pos = this.pos;
    while (pos + ratio <= merged.length) {
      const a = Math.floor(pos);
      const b = Math.max(a + 1, Math.floor(pos + ratio));
      let sum = 0;
      for (let i = a; i < b; i++) sum += merged[i];
      let v = sum / (b - a);
      v = v < -1 ? -1 : v > 1 ? 1 : v;
      this.out[this.outLen++] = v < 0 ? v * 0x8000 : v * 0x7fff;
      if (this.outLen === CHUNK) {
        this.port.postMessage(this.out.buffer, [this.out.buffer]);
        this.out = new Int16Array(CHUNK);
        this.outLen = 0;
      }
      pos += ratio;
    }
    const consumed = Math.floor(pos);
    this.buf = merged.slice(consumed);
    this.pos = pos - consumed;
    return true;
  }
}

registerProcessor("pcm-downsampler", PcmDownsampler);

// Dashboard sounds (Web Audio oscillators, no files). Shared by index.html and
// sounds.html. sfx.<name>() plays into `target`: a live AudioContext on the
// test page (created on a click), or an OfflineAudioContext when renderWav()
// turns a sound into a WAV. Rendering needs no click, so the dashboard renders
// a sound and hands it to server.py, which plays it on the Mac — browsers
// won't play anything on a page nobody has clicked.
let target = null;

function blip(freq, dur, type, gain, delay = 0) {
  const t0 = target.currentTime + delay;
  const osc = target.createOscillator(), g = target.createGain();
  osc.type = type;
  osc.frequency.value = freq;
  g.gain.setValueAtTime(0.0001, t0);
  g.gain.exponentialRampToValueAtTime(gain, t0 + 0.02);
  g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
  osc.connect(g).connect(target.destination);
  osc.start(t0);
  osc.stop(t0 + dur + 0.03);
}

const sfx = {
  fail()  { blip(196, 0.32, 'sawtooth', 0.25); blip(147, 0.32, 'sawtooth', 0.25, 0.2); },   // G3 → D3 buzz
  qg()    { [0, 0.16, 0.32].forEach(at => blip(110, 0.12, 'square', 0.15, at)); },           // three low knocks
  happy() { [523, 659, 784, 1047].forEach((f, i) => blip(f, 0.14, 'triangle', 0.2, i * 0.09)); }, // C-E-G-C
  approved() { blip(880, 0.35, 'triangle', 0.22); blip(1319, 0.45, 'triangle', 0.18, 0.14); },       // bell ding-ding
  qgpass() { blip(1175, 0.06, 'sine', 0.2); blip(1568, 0.08, 'sine', 0.2, 0.08); },                  // quick high tick-tick
  done()  { blip(587, 0.09, 'sine', 0.2); blip(880, 0.14, 'sine', 0.2, 0.1); },                      // D5 → A5, build finished green
};

// One sound as a 16-bit mono WAV (an ArrayBuffer). One second covers them all.
async function renderWav(name) {
  const rate = 44100;
  const prev = target;
  target = new OfflineAudioContext(1, rate, rate);
  const ctx = target;
  try { sfx[name](); } finally { target = prev; }
  const pcm = (await ctx.startRendering()).getChannelData(0);
  const v = new DataView(new ArrayBuffer(44 + pcm.length * 2));
  const tag = (at, s) => [...s].forEach((c, i) => v.setUint8(at + i, c.charCodeAt(0)));
  tag(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); tag(8, 'WAVE');
  tag(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  tag(36, 'data'); v.setUint32(40, pcm.length * 2, true);
  pcm.forEach((x, i) => v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, x)) * 0x7fff, true));
  return v.buffer;
}

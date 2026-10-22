// Sarma senaryoları: klipler gecikmeli geliyor (yavaş bağlantı), hiçbir cümle kaçmamalı.
// Çalıştır: node tests/seek-test.js
const assert = require('assert');
const path = require('path');
let now = 0;
global.performance = { now: () => now };
global.setInterval = () => 1;
global.clearInterval = () => {};
global.window = globalThis;

const started = [];
let currentStartId = null;
class FakeAudio {
  constructor() { this.paused = true; this.ended = false; this.currentTime = 0; this.duration = 4; this.playbackRate = 1; this.volume = 1; }
  addEventListener(ev, fn) { if (ev === 'canplay') Promise.resolve().then(fn); }
  set src(v) {} load() {} removeAttribute() {}
  play() { this.paused = false; started.push({ at: +media.t.toFixed(2), offset: +this.currentTime.toFixed(2), id: currentStartId }); return Promise.resolve(); }
  pause() { this.paused = true; }
}
global.Audio = FakeAudio;
require(path.join(__dirname, '..', 'extension', 'sync-player.js'));

const media = {
  t: 0, playing: false, rate: 1, vol: 1,
  get currentTime() { return this.t; }, get paused() { return !this.playing; }, get userPaused() { return !this.playing; },
  get playbackRate() { return this.rate; }, get volume() { return this.vol; }, set volume(v) { this.vol = v; },
  play() { this.playing = true; }, pause() { this.playing = false; }, seek(t) { this.t = t; },
};

let LATENCY = 1200;   // ms, sahte zamanda
let failNext = 0;     // geçici hata taklidi
const inflight = [];
const overlay = { text: '', status: null, setText(a) { this.text = a; }, setStatus(s) { this.status = s; } };
const p = new window.YouCev.SyncPlayer(media, {
  overlay,
  fetchClip: () => new Promise((resolve, reject) => {
    if (failNext > 0) { failNext--; inflight.push({ at: now + 100, fn: () => reject(new Error('bağlantı koptu')) }); return; }
    inflight.push({ at: now + LATENCY, fn: () => resolve(new Blob(['x'])) });
  }),
});
const origStart = p._startClip.bind(p);
p._startClip = (seg, clip, off) => { currentStartId = seg.id; return origStart(seg, clip, off); };

const flush = () => new Promise((r) => setTimeout(r, 0));
async function run(seconds) {
  for (let i = 0; i < seconds * 25; i++) {
    now += 40;
    if (media.playing) media.t += 0.04 * media.rate;
    for (let j = inflight.length - 1; j >= 0; j--) if (inflight[j].at <= now) { inflight[j].fn(); inflight.splice(j, 1); }
    p._tick();
    await flush();
  }
}

// Her 4 sn'de bir cümle, 3 sn konuşma; 0-300 sn arası çevrilmiş
const segs = [];
for (let i = 0; i < 75; i++) segs.push({ id: `s${i}`, start: i * 4 + 0.5, end: i * 4 + 3.5, en: 'x', tr: `cümle ${i}`, dur: 3.0, lead: 0.4, audio: true });
p.addSegments(segs);
p.setProgress({ state: 'processing', ready: [[0, 300]], done: false, duration: 600, percent: 50, message: '' });

(async () => {
  media.play();
  await run(10);

  // 1) İleri sar: bir cümle başlamadan 0.3 sn önceye (s25 = 100.5) — ses 1.2 sn geç geliyor
  started.length = 0;
  media.t = 100.2;
  await run(9);
  const ids = started.map((s) => s.id);
  console.log('ileri sarma sonrası:', JSON.stringify(started));
  assert.ok(ids.includes('s25'), 's25 atlanmamalı (gecikmeli de olsa çalmalı)');
  const s25 = started.find((s) => s.id === 's25');
  assert.ok(s25.at >= 100.5 && s25.at < 102, 'gelince hemen başlamalı');
  assert.ok(s25.offset > 0.4 + 0.5, 'kaçan baş kısmı atlanmalı (senkron): ' + s25.offset);
  assert.ok(ids.includes('s26') && ids.includes('s27'), 'sonraki cümleler çalmalı');

  // 2) Geri sar (klipler bellekten atılmış olabilir)
  started.length = 0;
  media.t = 20.3;
  await run(9);
  console.log('geri sarma sonrası:', JSON.stringify(started));
  assert.ok(started.some((s) => s.id === 's5'), 's5 çalmalı');
  assert.ok(started.some((s) => s.id === 's6') && started.some((s) => s.id === 's7'), 's6, s7 çalmalı');

  // 3) Geçici hatalar: iki yükleme başarısız olsa da tekrar denenip çalmalı
  started.length = 0;
  LATENCY = 300;
  failNext = 2;
  media.t = 200.0;
  await run(9);
  console.log('geçici hata sonrası:', JSON.stringify(started));
  assert.ok(started.some((s) => s.id === 's50'), 's50 tekrar denenip çalmalı');
  assert.ok(started.some((s) => s.id === 's51'), 's51 çalmalı');

  // 4) Çevrilmemiş yere sarma → beklet; hazır olunca o cümleden başla
  started.length = 0;
  media.t = 310.2;
  await run(1);
  assert.strictEqual(p.autoPaused, true, 'çevrilmemiş yerde beklemeli');
  assert.strictEqual(media.playing, false);
  const more = [];
  for (let i = 75; i < 100; i++) more.push({ id: `s${i}`, start: i * 4 + 0.5, end: i * 4 + 3.5, en: 'x', tr: `cümle ${i}`, dur: 3.0, lead: 0.4, audio: true });
  p.addSegments(more);
  p.setProgress({ state: 'processing', ready: [[0, 300], [300, 400]], done: false, duration: 600, percent: 66, message: '' });
  await run(6);
  console.log('hazır olunca:', JSON.stringify(started));
  assert.strictEqual(media.playing, true);
  assert.ok(started.some((s) => s.id === 's77'), 's77 (310.5) çalmalı');
  console.log('SARMA TESTLERİ GEÇTİ');
  process.exit(0);
})().catch((e) => { console.error('BAŞARISIZ:', e.message); process.exit(1); });

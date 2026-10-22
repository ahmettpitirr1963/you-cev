// SyncPlayer mantık testi: sahte video + sahte zaman.  Çalıştır: node tests/player-test.js
const assert = require('assert');
const path = require('path');
let now = 0;
global.performance = { now: () => now };
global.setInterval = () => 1;
global.clearInterval = () => {};
global.window = globalThis;

class FakeAudio {
  constructor() { this.paused = true; this.ended = false; this.currentTime = 0; this.duration = 3; this.playbackRate = 1; this.volume = 1; }
  addEventListener(ev, fn) { if (ev === 'canplay') Promise.resolve().then(fn); }
  set src(v) {} load() {} removeAttribute() {}
  play() { this.paused = false; FakeAudio.started.push({ at: media.t, offset: this.currentTime }); return Promise.resolve(); }
  pause() { this.paused = true; }
}
FakeAudio.started = [];
global.Audio = FakeAudio;

require(path.join(__dirname, '..', 'extension', 'sync-player.js'));

const media = {
  t: 0, playing: false, rate: 1, vol: 1,
  get currentTime() { return this.t; }, get paused() { return !this.playing; }, get userPaused() { return !this.playing; },
  get playbackRate() { return this.rate; }, get volume() { return this.vol; }, set volume(v) { this.vol = v; },
  play() { this.playing = true; }, pause() { this.playing = false; }, seek(t) { this.t = t; },
};
const overlay = { text: '', status: null, setText(a) { this.text = a; }, setStatus(s) { this.status = s; } };
const p = new window.YouCev.SyncPlayer(media, {
  overlay, fetchClip: () => Promise.resolve(new Blob(['x'], { type: 'audio/mpeg' })), duck: 0.2,
});
const flush = () => new Promise((r) => setTimeout(r, 0));
async function run(seconds, playing = true) {
  for (let i = 0; i < seconds * 25; i++) {
    now += 40;
    if (media.playing && playing) media.t += 0.04 * media.rate;
    p._tick();
    if (i % 10 === 0) await flush();
  }
}

(async () => {
  // 1) Video başlamamış, sunucudan bilgi yok: bekletme/uyarı yok
  await run(1);
  assert.strictEqual(p.autoPaused, false); assert.strictEqual(p.userOverride, false);

  // 2) Kullanıcı oynattı, sunucu bağlandı ama hiçbir şey hazır değil → otomatik bekletme
  media.play();
  p.setProgress({ state: 'processing', ready: [], done: false, duration: 300, percent: 0, message: 'Ses indiriliyor' });
  await run(0.2);
  assert.strictEqual(p.autoPaused, true, 'hazır değilken beklemeli'); assert.strictEqual(media.playing, false);
  assert.ok(overlay.status.includes('bekletildi'), overlay.status);

  // 3) İlk parça geldi → devam, ilk cümle tam zamanında başlasın
  p.addSegments([
    { id: '0000_000', start: 0.5, end: 3, en: 'a', tr: 'bir', dur: 2.2, lead: 0.4, audio: true },
    { id: '0000_001', start: 5.0, end: 9, en: 'b', tr: 'iki', dur: 3.0, lead: 0.4, audio: true },
    { id: '0000_002', start: 9.2, end: 14, en: 'c', tr: 'üç', dur: 4.0, lead: 0.4, audio: true },
  ]);
  p.setProgress({ state: 'processing', ready: [[0, 20]], done: false, duration: 300, percent: 7, message: '' });
  await run(0.5);
  assert.strictEqual(p.autoPaused, false); assert.strictEqual(media.playing, true);
  await run(6);
  const starts = FakeAudio.started.map((s) => ({ at: +s.at.toFixed(2), offset: +s.offset.toFixed(2) }));
  console.log('klip başlangıçları:', JSON.stringify(starts));
  assert.ok(Math.abs(starts[0].at - 0.5) < 0.1, 'ilk cümle 0.5 sn civarında başlamalı');
  assert.ok(Math.abs(starts[0].offset - 0.4) < 0.01, 'baştaki sessizlik atlanmalı');
  assert.ok(Math.abs(starts[1].at - 5.0) < 0.1, 'ikinci cümle 5.0 sn civarında');
  assert.ok(overlay.text, 'altyazı görünmeli');
  assert.ok(media.vol < 0.5, 'konuşurken orijinal ses kısılmalı: ' + media.vol);

  // 4) Hazır bölgenin sonuna yaklaşınca beklet (t > 17)
  await run(11);
  assert.strictEqual(p.autoPaused, true, 'hazır bölge bitmeden beklemeli, t=' + media.t.toFixed(1));
  assert.ok(media.t > 16.5 && media.t < 17.2, 't=' + media.t);

  // 5) Kullanıcı bekleme sırasında kendisi oynatırsa zorlama
  now += 2000; media.play();
  await run(1);
  assert.strictEqual(p.autoPaused, false); assert.strictEqual(p.userOverride, true); assert.strictEqual(media.playing, true);
  assert.ok(overlay.status.includes('geride'), overlay.status);

  // 6) Çeviri yetişince uyarı kalkar
  p.setProgress({ state: 'processing', ready: [[0, 80]], done: false, duration: 300, percent: 25, message: '' });
  await run(0.5);
  assert.strictEqual(p.userOverride, false); assert.strictEqual(overlay.status, null);

  // 7) Duraklat: klip de durmalı; devam edince sürmeli
  p.addSegments([{ id: '0001_000', start: 20.0, end: 24, en: 'd', tr: 'dört', dur: 3.5, lead: 0.4, audio: true }]);
  await run(2.5);
  assert.ok(p.current && p.current.clip.playing, 'klip çalıyor olmalı t=' + media.t.toFixed(1));
  media.pause(); await run(0.5, false);
  assert.strictEqual(p.current.clip.playing, false, 'video durunca klip durmalı');
  media.play(); await run(0.2);
  assert.strictEqual(p.current.clip.playing, true, 'video devam edince klip sürmeli');

  // 8) İleri sarma: cümlenin ortasına → kaldığı yerden çal
  FakeAudio.started.length = 0;
  media.t = 6.5; await run(0.3);
  const s = FakeAudio.started[0];
  console.log('sarma sonrası:', JSON.stringify(s));
  assert.ok(s && Math.abs(s.offset - (0.4 + 1.5)) < 0.2, 'cümle ortasından devam etmeli');

  // 9) Reklam: hiçbir şey çalmasın, ses geri açılsın
  p.opts.isSuspended = () => true; await run(0.5);
  assert.strictEqual(p.current, null); assert.strictEqual(overlay.text, '');
  console.log('TÜM TESTLER GEÇTİ');
  process.exit(0);
})().catch((e) => { console.error('BAŞARISIZ:', e.message); process.exit(1); });

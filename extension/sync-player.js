/* You-Cev senkron oynatıcı.
 * Türkçe klipleri ve altyazıyı videonun zamanına (currentTime) göre oynatır.
 * Chrome eklentisi ve yerel web oynatıcı aynı dosyayı kullanır. */
(function (global) {
  'use strict';

  const TICK_MS = 40;
  const WAIT_PREVIOUS = 0.6;   // önceki klip bu kadar içinde bitecekse kesme, bekle
  const PRELOAD_AHEAD = 40;    // sn ileriye kadar klipleri önceden yükle
  const PAUSE_MARGIN = 3;      // hazır bölge bu kadar azalınca videoyu beklet
  const RESUME_MARGIN = 12;    // bu kadar hazır olunca devam et
  const REWIND_WINDOW = 3;     // bekleme sonrası, son 3 sn'de başlamış cümleye geri sar
  const MAX_LOAD_TRIES = 5;
  const DUCK_RAMP_MS = 250;
  const DUCK_HOLD_MS = 600;

  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

  /* ---------- Klip türleri ---------- */

  // HTMLAudioElement: hız değişince perdeyi korur (preservesPitch)
  class HtmlClip {
    constructor(blob) {
      this.url = URL.createObjectURL(blob);
      this.endAt = null;  // konuşmanın bittiği an (sondaki sessizlik sayılmaz)
      this.el = new Audio();
      this.el.preload = 'auto';
      this.ready = new Promise((resolve, reject) => {
        this.el.addEventListener('canplay', () => resolve(this), { once: true });
        this.el.addEventListener('error', () => reject(this.el.error || new Error('audio error')), { once: true });
      });
      this.el.src = this.url;
      this.el.load();
    }
    play(offset, rate, volume) {
      this.el.currentTime = offset;
      this.el.playbackRate = rate;
      this.el.volume = volume;
      return this.el.play();
    }
    pause() { this.el.pause(); }
    resume() { return this.el.play(); }
    stop() { this.el.pause(); }
    get playing() { return !this.el.paused && !this.el.ended; }
    get ended() { return this.el.ended; }
    get remaining() { return Math.max(0, (this.endAt || this.el.duration || 0) - this.el.currentTime); }
    get rate() { return this.el.playbackRate; }
    setRate(r) { this.el.playbackRate = r; }
    setVolume(v) { this.el.volume = v; }
    destroy() {
      this.el.pause();
      this.el.removeAttribute('src');
      URL.revokeObjectURL(this.url);
    }
  }

  // Web Audio: sayfa politikası <audio> blob'larını engellerse yedek
  class WebAudioClip {
    constructor(blob, ctx) {
      this.ctx = ctx;
      this.gain = ctx.createGain();
      this.gain.connect(ctx.destination);
      this.src = null;
      this.endAt = null;
      this.offset = 0;
      this.startedAt = 0;
      this._rate = 1;
      this._playing = false;
      this._ended = false;
      this.ready = blob.arrayBuffer()
        .then((ab) => ctx.decodeAudioData(ab))
        .then((buf) => { this.buffer = buf; return this; });
    }
    _pos() {
      return this._playing ? this.offset + (this.ctx.currentTime - this.startedAt) * this._rate : this.offset;
    }
    play(offset, rate, volume) {
      this.stop();
      if (this.ctx.state === 'suspended') this.ctx.resume();
      const s = this.ctx.createBufferSource();
      s.buffer = this.buffer;
      s.playbackRate.value = rate;
      s.connect(this.gain);
      this.gain.gain.value = volume;
      s.onended = () => { if (this.src === s) { this._playing = false; this._ended = true; this.src = null; } };
      this.offset = Math.min(offset, this.buffer.duration);
      this._rate = rate;
      s.start(0, this.offset);
      this.src = s;
      this.startedAt = this.ctx.currentTime;
      this._playing = true;
      this._ended = false;
      return Promise.resolve();
    }
    pause() {
      if (!this._playing) return;
      this.offset = this._pos();
      const s = this.src;
      this.src = null;
      this._playing = false;
      try { s.stop(); } catch (e) { /* zaten durmuş */ }
    }
    resume() { return this.play(this.offset, this._rate, this.gain.gain.value); }
    stop() {
      const s = this.src;
      this.src = null;
      this._playing = false;
      if (s) { try { s.stop(); } catch (e) { /* zaten durmuş */ } }
    }
    get playing() { return this._playing; }
    get ended() { return this._ended; }
    get remaining() { return this.buffer ? Math.max(0, (this.endAt || this.buffer.duration) - this._pos()) : 0; }
    get rate() { return this._rate; }
    setRate(r) {
      if (this._playing) { this.offset = this._pos(); this.startedAt = this.ctx.currentTime; }
      this._rate = r;
      if (this.src) this.src.playbackRate.value = r;
    }
    setVolume(v) { this.gain.gain.value = v; }
    destroy() { this.stop(); this.gain.disconnect(); }
  }

  /* ---------- Medya bağdaştırıcıları ---------- */

  function videoAdapter(video) {
    return {
      element: video,
      get currentTime() { return video.currentTime; },
      // Arabelleğe alırken (readyState < 3) video ilerlemez: klip de beklemeli
      get paused() { return video.paused || video.ended || video.readyState < 3; },
      get userPaused() { return video.paused; },
      get playbackRate() { return video.playbackRate; },
      get duration() { return video.duration; },
      get volume() { return video.volume; },
      set volume(v) { video.volume = v; },
      get muted() { return video.muted; },
      play() { return video.play(); },
      pause() { video.pause(); },
      seek(t) { video.currentTime = t; },
    };
  }

  /* ---------- Oynatıcı ---------- */

  class SyncPlayer {
    /**
     * @param media  videoAdapter(...) veya aynı arayüzü sunan nesne
     * @param opts   { overlay: {setText(tr,en), setStatus(text|null)}, fetchClip(seg) -> Promise<Blob>,
     *                 onPosition(t), isSuspended() -> bool, duck, voiceVolume, showEnglish }
     */
    constructor(media, opts) {
      this.media = media;
      this.opts = Object.assign({ duck: 0.15, voiceVolume: 1, showEnglish: false }, opts);
      this.overlay = this.opts.overlay;
      this.segments = [];
      this.byId = new Map();
      this.ready = [];
      this.complete = false;
      this.duration = 0;
      this.linked = false;       // sunucudan ilerleme gelmeden videoyu bekletme
      this.enabled = true;
      this.clips = new Map();
      this.failures = new Map();
      this.loading = 0;
      this.current = null;
      this.cursor = 0;
      this.resumeSeg = null;
      this.lastT = 0;
      this.lastTickAt = performance.now();
      this.lastPosSent = 0;
      this.autoPaused = false;
      this.userOverride = false;
      this._ourPlayUntil = 0;
      this.baseVolume = null;
      this.lastSetVolume = null;
      this.duckGain = 1;
      this.duckHoldUntil = 0;
      this.mode = 'html';
      this.ctx = null;
      this.shownSub = null;
      this.shownStatus = undefined;
      this.message = '';
      this.timer = setInterval(() => this._tick(), TICK_MS);
      this._onSeek(this._time());
    }

    /* ----- dışarıdan çağrılanlar ----- */

    addSegments(items) {
      for (const s of items) this.byId.set(s.id, s);
      this.segments = Array.from(this.byId.values()).sort((a, b) => a.start - b.start);
      if (!this.current && !this.resumeSeg) this._findResume(this._time());
    }

    setProgress(p) {
      this.linked = p.state !== 'error';
      this.ready = p.ready || [];
      this.complete = !!p.done;
      if (p.duration) this.duration = p.duration;
      this.message = p.state === 'error' ? ('You-Cev hatası: ' + (p.error || '')) : (p.message || '');
      this.percent = p.percent || 0;
    }

    setLinked(v) { this.linked = v; }

    reset() {
      this._stopCurrent();
      for (const e of this.clips.values()) if (e.clip) e.clip.destroy();
      this.clips.clear();
      this.failures.clear();
      this.byId.clear();
      this.segments = [];
      this.ready = [];
      this.complete = false;
      this.linked = false;
      this._onSeek(this._time());
    }

    setOptions(o) { Object.assign(this.opts, o); this.shownSub = null; }

    setEnabled(v) {
      this.enabled = v;
      if (!v) this._suspend();
    }

    destroy() {
      clearInterval(this.timer);
      this._suspend();
      this._applyDuck(false, true);
      for (const e of this.clips.values()) if (e.clip) e.clip.destroy();
      this.clips.clear();
      if (this.ctx) this.ctx.close();
    }

    /* ----- iç işleyiş ----- */

    _time() {
      try { const t = this.media.currentTime; return isFinite(t) ? t : 0; } catch (e) { return 0; }
    }

    _suspend() {
      this._stopCurrent();
      if (this.autoPaused) { this.autoPaused = false; this._play(); }
      this._setSub(null);
      this._setStatus(null);
    }

    _tick() {
      const m = this.media;
      const now = performance.now();
      const t = this._time();
      const dt = (now - this.lastTickAt) / 1000;
      this.lastTickAt = now;

      if (!this.enabled || (this.opts.isSuspended && this.opts.isSuspended())) {
        // Reklam vb.: hiçbir şey çalma, sesi geri aç
        this._stopCurrent();
        this._applyDuck(false);
        this._setSub(null);
        this._setStatus(null);
        this.lastT = t;
        return;
      }

      const expected = this.lastT + (m.paused ? 0 : dt * m.playbackRate);
      const jumped = Math.abs(t - expected) > 1.0;
      if (jumped) this._onSeek(t);
      this.lastT = t;

      if (jumped || now - this.lastPosSent > 1000) {
        this.lastPosSent = now;
        if (this.opts.onPosition) this.opts.onPosition(t);
      }

      this._preload(t);
      this._readiness(t);
      this._updateSubtitle(t);
      this._audio(t);
      const voiceActive = !!(this.current && this.current.clip.playing);
      if (voiceActive) this.duckHoldUntil = now + DUCK_HOLD_MS;
      this._applyDuck(voiceActive || now < this.duckHoldUntil);
    }

    _onSeek(t) {
      this._stopCurrent();
      this.userOverride = false;
      this.cursor = t;
      this.resumeSeg = null;
      this._findResume(t);
    }

    // Ortasına sarılan cümleyi kaldığı yerden çal (kaçırma olmasın)
    _findResume(t) {
      const s = this._segmentAt(t);
      if (s && s.audio && s.start < this.cursor) {
        const len = s.dur || (s.end - s.start);
        if (t - s.start < len * 0.7) this.resumeSeg = s;
      }
    }

    _segmentAt(t) {
      let lo = 0, hi = this.segments.length - 1, found = null;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (this.segments[mid].start <= t) { found = this.segments[mid]; lo = mid + 1; } else hi = mid - 1;
      }
      return found;
    }

    _firstAtOrAfter(t) {
      let lo = 0, hi = this.segments.length;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (this.segments[mid].start < t) lo = mid + 1; else hi = mid;
      }
      return this.segments[lo] || null;
    }

    _readyEndAt(t) {
      for (const [a, b] of this.ready) if (t >= a - 0.05 && t <= b + 0.05) return b;
      return -1;
    }

    _play() {
      this._ourPlayUntil = performance.now() + 1500;
      const p = this.media.play();
      if (p && p.catch) p.catch(() => {});
    }

    _readiness(t) {
      const m = this.media;
      if (!this.linked) {
        if (this.autoPaused) { this.autoPaused = false; this._play(); }
        this._setStatus(this.message && this.message.startsWith('You-Cev') ? this.message : null);
        return;
      }
      const end = this.duration || Infinity;
      const readyEnd = this._readyEndAt(t);
      const ok = (r) => this.complete || readyEnd >= Math.min(end, t + r) - 0.05;

      // Biz beklettik ama kullanıcı oynattı: zorlamayalım
      if (this.autoPaused && !m.userPaused && performance.now() > this._ourPlayUntil) {
        this.autoPaused = false;
        this.userOverride = true;
      }

      if (!ok(PAUSE_MARGIN)) {
        if (!this.autoPaused && !this.userOverride && !m.userPaused) {
          this.autoPaused = true;
          m.pause();
          console.info('[You-Cev] bu bölüm henüz çevrilmedi, video bekletildi', t.toFixed(1));
        }
        this._setStatus(this.userOverride
          ? 'Çeviri geride kaldı — bu kısım sessiz geçebilir. Tam çeviri için videoyu birkaç saniye duraklatın.'
          : 'Bu bölüm çevriliyor, video birkaç saniye bekletildi… (oynatmaya basmayın)');
      } else if (this.autoPaused) {
        if (ok(RESUME_MARGIN)) {
          this.autoPaused = false;
          const s = this._segmentAt(t);
          if (s && t - s.start < REWIND_WINDOW && t - s.start > 0.2) {
            m.seek(Math.max(0, s.start - 0.1));
            this._onSeek(Math.max(0, s.start - 0.1));
          } else {
            this._onSeek(t);
          }
          this._play();
          this._setStatus(null);
        }
      } else {
        if (this.userOverride && ok(RESUME_MARGIN)) this.userOverride = false;
        this._setStatus(null);
      }
    }

    _preload(t) {
      for (const [id, e] of this.clips) {
        const s = this.byId.get(id);
        const keep = s && s.start + (s.dur || 0) > t - 10 && s.start < t + PRELOAD_AHEAD * 2;
        if (!keep && (!this.current || this.current.seg.id !== id)) {
          if (e.clip) e.clip.destroy();
          this.clips.delete(id);
        }
      }
      const startIdx = this.segments.indexOf(this._segmentAt(t - 2) || this.segments[0]);
      for (let i = Math.max(0, startIdx); i < this.segments.length && this.loading < 4; i++) {
        const s = this.segments[i];
        if (s.start > t + PRELOAD_AHEAD) break;
        if (s.audio && !this.clips.has(s.id) && this._canLoad(s)) this._load(s);
      }
    }

    _canLoad(seg) {
      const f = this.failures.get(seg.id);
      return !f || (f.n < MAX_LOAD_TRIES && performance.now() >= f.retryAt);
    }

    _gaveUp(seg) {
      const f = this.failures.get(seg.id);
      return !!f && f.n >= MAX_LOAD_TRIES;
    }

    _makeClip(blob) {
      if (this.mode === 'webaudio') {
        if (!this.ctx) this.ctx = new (global.AudioContext || global.webkitAudioContext)();
        return new WebAudioClip(blob, this.ctx);
      }
      return new HtmlClip(blob);
    }

    _load(seg) {
      const entry = { loaded: false, clip: null };
      this.clips.set(seg.id, entry);
      this.loading++;
      let blob = null;
      this.opts.fetchClip(seg)
        .then((b) => { blob = b; return this._makeClip(b).ready; })
        .catch((err) => {
          // <audio> blob'u engellendiyse Web Audio'ya geç ve tekrar dene
          if (blob && this.mode === 'html') {
            console.warn('[You-Cev] <audio> kullanılamadı, Web Audio moduna geçiliyor', err);
            this.mode = 'webaudio';
            return this._makeClip(blob).ready;
          }
          throw err;
        })
        .then((clip) => {
          if (this.clips.get(seg.id) !== entry) { clip.destroy(); return; }
          entry.clip = clip;
          entry.loaded = true;
        })
        .catch((err) => {
          // Geçici hatalar (bağlantı kopması vb.) kalıcı sayılmasın: artan aralıklarla tekrar dene
          const n = ((this.failures.get(seg.id) || {}).n || 0) + 1;
          this.failures.set(seg.id, { n, retryAt: performance.now() + 1000 * n });
          console.warn('[You-Cev] klip yüklenemedi', seg.id, n, err && err.message);
          if (this.clips.get(seg.id) === entry) this.clips.delete(seg.id);
        })
        .finally(() => { this.loading--; });
    }

    _startClip(seg, clip, offset) {
      const rate = this.media.playbackRate || 1;
      // edge-tts klibin başına sessizlik ekler: atla ki ses cümleyle aynı anda başlasın
      const lead = seg.lead || 0;
      clip.endAt = seg.dur ? lead + seg.dur + 0.05 : null;
      clip.play(lead + offset, rate, this.opts.voiceVolume).catch((err) => {
        console.warn('[You-Cev] oynatılamadı', err);
        if (this.mode === 'html' && err && err.name === 'NotSupportedError') this.mode = 'webaudio';
      });
      this.current = { seg, clip, pausedByVideo: false };
    }

    _stopCurrent() {
      if (this.current) {
        this.current.clip.stop();
        this.current = null;
      }
    }

    _audio(t) {
      const m = this.media;
      const c = this.current;
      if (m.paused) {
        if (c && c.clip.playing) { c.clip.pause(); c.pausedByVideo = true; }
        return;
      }
      const rate = m.playbackRate || 1;
      if (c) {
        if (c.pausedByVideo) {
          c.pausedByVideo = false;
          if (c.clip.remaining > 0.05) { c.clip.setRate(rate); c.clip.resume().catch(() => {}); }
        }
        if (Math.abs(c.clip.rate - rate) > 0.01) c.clip.setRate(rate);
        c.clip.setVolume(this.opts.voiceVolume);
        if (!c.pausedByVideo && (c.clip.ended || c.clip.remaining <= 0.02)) {
          c.clip.stop();
          this.current = null;
        }
      }

      if (this.resumeSeg && !this.current) {
        const s = this.resumeSeg;
        const e = this.clips.get(s.id);
        const off = t - s.start;
        if (off > (s.dur || s.end - s.start) * 0.7 || off < 0) {
          this.resumeSeg = null;
        } else if (e && e.loaded) {
          this.resumeSeg = null;
          this._startClip(s, e.clip, Math.max(0, off));
        }
      }

      const seg = this._firstAtOrAfter(this.cursor);
      if (!seg || seg.start > t + 0.03) return;
      const late = t - seg.start;
      const len = seg.dur || (seg.end - seg.start);
      if (late > Math.max(1.0, len * 0.6) || !seg.audio || this._gaveUp(seg)) {
        // Çok geç kaldık / sesi yok: atla (altyazı yine görünür)
        if (seg.audio) console.info('[You-Cev] cümle atlandı', seg.id, `${late.toFixed(1)} sn geç`);
        this.cursor = seg.start + 1e-3;
        return;
      }
      if (this.current && this.current.clip.playing) {
        // Önceki cümle bitmek üzereyse kısa bekle; değilse kes (kayma birikmesin)
        if (this.current.clip.remaining <= WAIT_PREVIOUS && late < WAIT_PREVIOUS) return;
        this._stopCurrent();
      }
      const e = this.clips.get(seg.id);
      if (!e || !e.loaded) {
        // Ses henüz gelmedi (ör. sarmadan hemen sonra): atlamak yerine bekle; yukarıdaki
        // sınır aşılırsa atlanır. Gelince kaçan baş kısmı atlanarak senkron başlar.
        if (!e && this._canLoad(seg)) this._load(seg);
        return;
      }
      this.cursor = seg.start + 1e-3;
      this.resumeSeg = null;
      this._startClip(seg, e.clip, late > WAIT_PREVIOUS ? Math.max(0, late - 0.15) : 0);
    }

    _updateSubtitle(t) {
      const s = this._segmentAt(t);
      let show = null;
      if (s) {
        const next = this._firstAtOrAfter(s.start + 1e-3);
        let until = Math.max(s.end, s.start + (s.dur || 0)) + 0.6;
        if (next) until = Math.min(until, next.start);
        if (t < until) show = s;
      }
      this._setSub(show);
    }

    _setSub(s) {
      const key = s ? s.id + (this.opts.showEnglish ? 'e' : '') : null;
      if (key === this.shownSub) return;
      this.shownSub = key;
      this.overlay.setText(s ? s.tr : '', s && this.opts.showEnglish ? s.en : '');
    }

    _setStatus(text) {
      if (text === this.shownStatus) return;
      this.shownStatus = text;
      this.overlay.setStatus(text);
    }

    _applyDuck(active, restore) {
      const m = this.media;
      let v;
      try { v = m.volume; } catch (e) { return; }
      if (typeof v !== 'number' || isNaN(v)) return;
      // Kullanıcı ses seviyesini değiştirdiyse yeni taban seviye odur
      if (this.lastSetVolume !== null && Math.abs(v - this.lastSetVolume) > 0.02) {
        this.baseVolume = this.duckGain < 0.999 ? clamp(v / this.duckGain, 0, 1) : v;
      }
      if (this.baseVolume === null) this.baseVolume = v;
      const target = restore ? 1 : (active ? clamp(this.opts.duck, 0, 1) : 1);
      const step = restore ? 1 : TICK_MS / DUCK_RAMP_MS;
      this.duckGain = this.duckGain < target
        ? Math.min(target, this.duckGain + step)
        : Math.max(target, this.duckGain - step);
      const nv = clamp(this.baseVolume * this.duckGain, 0, 1);
      if (Math.abs(nv - v) > 0.004) {
        try { m.volume = nv; } catch (e) { /* yoksay */ }
        this.lastSetVolume = nv;
      } else {
        this.lastSetVolume = v;
      }
    }
  }

  global.YouCev = { SyncPlayer, videoAdapter };
})(typeof window !== 'undefined' ? window : globalThis);

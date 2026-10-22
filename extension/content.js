/* YouTube sayfasında çalışır: videoyu bulur, altyazı katmanını ekler, SyncPlayer'ı bağlar. */
(() => {
  'use strict';

  const DEFAULTS = { enabled: true, duck: 0.15, voiceVolume: 1, subtitleSize: 26, showEnglish: false };
  const st = {
    settings: { ...DEFAULTS },
    videoId: null,
    video: null,
    player: null,
    overlay: null,
    port: null,
    jobKey: null,
    reqId: 0,
    pending: new Map(),
    statusError: null,
  };

  /* ---------- Altyazı katmanı ---------- */

  function createOverlay(container) {
    const root = document.createElement('div');
    root.className = 'youcev-overlay';
    root.innerHTML = '<div class="youcev-status" hidden></div>' +
      '<div class="youcev-sub" hidden><span class="youcev-tr"></span><span class="youcev-en"></span></div>';
    container.appendChild(root);
    const status = root.querySelector('.youcev-status');
    const sub = root.querySelector('.youcev-sub');
    const tr = root.querySelector('.youcev-tr');
    const en = root.querySelector('.youcev-en');
    const api = {
      root,
      setText(trText, enText) {
        tr.textContent = trText || '';
        en.textContent = enText || '';
        en.hidden = !enText;
        sub.hidden = !trText;
      },
      setStatus(text) {
        const shown = text || st.statusError;
        status.textContent = shown || '';
        status.hidden = !shown;
      },
      applySettings(s) { root.style.setProperty('--youcev-size', `${s.subtitleSize}px`); },
      remove() { root.remove(); },
    };
    api.applySettings(st.settings);
    return api;
  }

  /* ---------- Arka planla iletişim ---------- */

  function send(msg) {
    if (!st.port) connect();
    try { st.port.postMessage(msg); } catch (e) { st.port = null; }
  }

  function connect() {
    try {
      st.port = chrome.runtime.connect({ name: 'youcev' });
    } catch (e) {
      // Eklenti yeniden yüklendi: bu sayfa artık eski eklentiye bağlı
      st.port = null;
      teardown();
      return;
    }
    st.port.onMessage.addListener(onMessage);
    st.port.onDisconnect.addListener(() => {
      st.port = null;
      for (const p of st.pending.values()) p.reject(new Error('bağlantı koptu'));
      st.pending.clear();
      setTimeout(() => { if (st.videoId) { send({ type: 'open', videoId: st.videoId }); } }, 1000);
    });
  }

  function onMessage(m) {
    if (m.type === 'clip') {
      const p = st.pending.get(m.reqId);
      if (!p) return;
      st.pending.delete(m.reqId);
      clearTimeout(p.timer);
      if (m.error) return p.reject(new Error(m.error));
      const bin = atob(m.b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      p.resolve(new Blob([bytes], { type: 'audio/mpeg' }));
    } else if (m.type === 'job') {
      if (st.jobKey && st.jobKey !== m.job.key && st.player) st.player.reset();
      st.jobKey = m.job.key;
    } else if (m.type === 'status') {
      st.statusError = m.ok ? null : m.error;
      if (st.player) {
        if (!m.ok) st.player.setLinked(false);
        st.player.shownStatus = undefined;  // durumu yeniden çiz
      }
      if (st.overlay) st.overlay.setStatus(null);
    } else if (m.type === 'ws' && st.player) {
      const msg = m.msg;
      if (msg.type === 'segments') st.player.addSegments(msg.items);
      else if (msg.type === 'progress') {
        st.player.setProgress(msg);
        if (msg.state === 'error') { st.statusError = 'You-Cev: ' + (msg.error || 'hata'); st.overlay.setStatus(null); }
      } else if (msg.type === 'warning') console.warn('[You-Cev]', msg.message);
    }
  }

  function fetchClip(seg) {
    return new Promise((resolve, reject) => {
      const reqId = ++st.reqId;
      const timer = setTimeout(() => { st.pending.delete(reqId); reject(new Error('zaman aşımı')); }, 15000);
      st.pending.set(reqId, { resolve, reject, timer });
      send({ type: 'clip', reqId, key: st.jobKey, segId: seg.id });
    });
  }

  /* ---------- Sayfa / video takibi ---------- */

  function currentVideoId() {
    if (location.pathname !== '/watch') return null;
    return new URLSearchParams(location.search).get('v');
  }

  function findVideo() {
    return document.querySelector('#movie_player video.html5-main-video') || document.querySelector('#movie_player video');
  }

  function isAdShowing() {
    const p = document.getElementById('movie_player');
    return !!(p && (p.classList.contains('ad-showing') || p.classList.contains('ad-interrupting')));
  }

  function teardown() {
    if (st.player) st.player.destroy();
    if (st.overlay) st.overlay.remove();
    if (st.videoId && st.port) { try { st.port.postMessage({ type: 'close' }); } catch (e) { /* yoksay */ } }
    st.player = null;
    st.overlay = null;
    st.video = null;
    st.videoId = null;
    st.jobKey = null;
    st.statusError = null;
  }

  function start(videoId, video) {
    st.videoId = videoId;
    st.video = video;
    st.overlay = createOverlay(document.getElementById('movie_player'));
    st.player = new window.YouCev.SyncPlayer(window.YouCev.videoAdapter(video), {
      overlay: st.overlay,
      fetchClip,
      onPosition: (t) => send({ type: 'position', t }),
      isSuspended: isAdShowing,
      duck: st.settings.duck,
      voiceVolume: st.settings.voiceVolume,
      showEnglish: st.settings.showEnglish,
    });
    send({ type: 'open', videoId });
  }

  function check() {
    const vid = st.settings.enabled ? currentVideoId() : null;
    const video = vid ? findVideo() : null;
    const stale = st.video && (!st.video.isConnected || st.video !== video);
    if (vid !== st.videoId || (st.videoId && stale)) {
      teardown();
      if (vid && video) start(vid, video);
    }
  }

  chrome.storage.sync.get(DEFAULTS, (s) => {
    st.settings = { ...DEFAULTS, ...s };
    check();
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'sync') return;
    for (const [k, v] of Object.entries(changes)) st.settings[k] = v.newValue;
    if (st.player) {
      st.player.setOptions({ duck: st.settings.duck, voiceVolume: st.settings.voiceVolume, showEnglish: st.settings.showEnglish });
    }
    if (st.overlay) st.overlay.applySettings(st.settings);
    check();
  });

  document.addEventListener('yt-navigate-finish', check);
  setInterval(check, 1000);
})();

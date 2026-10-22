/* Yerel web oynatıcı: YouTube linki (gömülü oynatıcı) veya yüklenen dosya. */
'use strict';

const $ = (id) => document.getElementById(id);
const DEFAULTS = { duck: 0.15, voiceVolume: 1, subtitleSize: 26, showEnglish: false };
const st = { settings: load(), player: null, ws: null, job: null, ytPlayer: null, retry: 0, wsTimer: null };

function load() {
  try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem('youcev') || '{}') }; } catch (e) { return { ...DEFAULTS }; }
}
function save() {
  try { localStorage.setItem('youcev', JSON.stringify(st.settings)); } catch (e) { /* gizli pencere */ }
}

/* ---------- Altyazı katmanı ---------- */

function createOverlay(container) {
  const root = document.createElement('div');
  root.className = 'youcev-overlay';
  root.innerHTML = '<div class="youcev-status" hidden></div>' +
    '<div class="youcev-sub" hidden><span class="youcev-tr"></span><span class="youcev-en"></span></div>';
  container.appendChild(root);
  const [status, sub, tr, en] = ['.youcev-status', '.youcev-sub', '.youcev-tr', '.youcev-en'].map((q) => root.querySelector(q));
  return {
    setText(a, b) { tr.textContent = a || ''; en.textContent = b || ''; en.hidden = !b; sub.hidden = !a; },
    setStatus(text) { status.textContent = text || ''; status.hidden = !text; },
    applySettings(s) { root.style.setProperty('--youcev-size', `${s.subtitleSize}px`); },
  };
}

/* ---------- YouTube gömülü oynatıcı bağdaştırıcısı ---------- */

function ytAdapter(p) {
  const S = window.YT.PlayerState;
  let lastRaw = -1, lastAt = 0;
  const playing = () => p.getPlayerState() === S.PLAYING;
  return {
    // getCurrentTime seyrek güncellenir; aradaki zamanı tahmin ederek akıcı hale getir
    get currentTime() {
      const raw = p.getCurrentTime() || 0;
      const now = performance.now();
      if (raw !== lastRaw) { lastRaw = raw; lastAt = now; return raw; }
      if (!playing()) return raw;
      return raw + Math.min(0.5, ((now - lastAt) / 1000) * (p.getPlaybackRate() || 1));
    },
    get paused() { return !playing(); },
    // Oynatma niyeti yoksa (başlamadı, duraklatıldı, bitti, hazırda) "duraklatılmış" say
    get userPaused() { const s = p.getPlayerState(); return s !== S.PLAYING && s !== S.BUFFERING; },
    get playbackRate() { return p.getPlaybackRate() || 1; },
    get duration() { return p.getDuration(); },
    get volume() { return p.getVolume() / 100; },
    set volume(v) { p.setVolume(Math.round(v * 100)); },
    get muted() { return p.isMuted(); },
    play() { p.playVideo(); },
    pause() { p.pauseVideo(); },
    seek(t) { p.seekTo(t, true); },
  };
}

let ytApi = null;
function loadYouTubeApi() {
  if (!ytApi) {
    ytApi = new Promise((resolve) => {
      window.onYouTubeIframeAPIReady = resolve;
      const s = document.createElement('script');
      s.src = 'https://www.youtube.com/iframe_api';
      document.head.appendChild(s);
    });
  }
  return ytApi;
}

/* ---------- Oturum ---------- */

function resetStage() {
  if (st.player) st.player.destroy();
  if (st.ytPlayer) st.ytPlayer.destroy();
  clearTimeout(st.wsTimer);
  if (st.ws) { const ws = st.ws; st.ws = null; ws.close(); }
  st.player = null;
  st.ytPlayer = null;
  st.job = null;
  setNotice('');
  $('stage').innerHTML = '';
  $('timeline').querySelectorAll('.r').forEach((e) => e.remove());
}

function attachPlayer(media) {
  const overlay = createOverlay($('stage'));
  overlay.applySettings(st.settings);
  st.overlay = overlay;
  st.player = new window.YouCev.SyncPlayer(media, {
    overlay,
    fetchClip: (seg) => fetch(`/api/clips/${encodeURIComponent(st.job.key)}/${seg.id}.mp3`).then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.blob();
    }),
    onPosition: (t) => {
      if (st.ws && st.ws.readyState === WebSocket.OPEN) st.ws.send(JSON.stringify({ type: 'position', t }));
      const d = st.player && st.player.duration;
      if (d) $('playhead').style.left = `${(t / d) * 100}%`;
    },
    duck: st.settings.duck,
    voiceVolume: st.settings.voiceVolume,
    showEnglish: st.settings.showEnglish,
  });
}

function connectWs() {
  const job = st.job;
  const ws = new WebSocket(`ws://${location.host}/ws/${encodeURIComponent(job.key)}`);
  st.ws = ws;
  ws.onopen = () => { st.retry = 0; };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (!st.player) return;
    if (msg.type === 'segments') st.player.addSegments(msg.items);
    else if (msg.type === 'meta') $('title').textContent = msg.title || '';
    else if (msg.type === 'progress') {
      st.player.setProgress(msg);
      drawTimeline(msg);
    } else if (msg.type === 'warning') $('state').textContent = msg.message;
  };
  ws.onclose = () => {
    if (st.ws !== ws) return;
    st.ws = null;
    st.player && st.player.setLinked(false);
    $('state').textContent = 'Sunucu bağlantısı koptu, yeniden bağlanılıyor…';
    st.wsTimer = setTimeout(reopen, Math.min(10000, 1000 * 2 ** st.retry++));
  };
}

async function reopen() {
  if (!st.job) return;
  try {
    // Sunucu yeniden başlamış olabilir: işi tekrar aç
    if (st.job.kind === 'youtube') st.job = await postJson('/api/jobs', { url: st.job.source_id });
    connectWs();
  } catch (e) {
    st.wsTimer = setTimeout(reopen, Math.min(10000, 1000 * 2 ** st.retry++));
  }
}

function drawTimeline(p) {
  const tl = $('timeline');
  tl.querySelectorAll('.r').forEach((e) => e.remove());
  if (p.duration) {
    for (const [a, b] of p.ready) {
      const r = document.createElement('div');
      r.className = 'r';
      r.style.left = `${(a / p.duration) * 100}%`;
      r.style.width = `${((b - a) / p.duration) * 100}%`;
      tl.appendChild(r);
    }
  }
  const text = p.state === 'error' ? `Hata: ${p.error}` : p.done ? 'Çeviri tamamlandı' : `%${Math.floor(p.percent)} · ${p.message}`;
  $('state').textContent = text;
  $('srt').hidden = !p.ready.length;
  $('srt').href = `/api/jobs/${encodeURIComponent(st.job.key)}/subtitles.srt`;
}

async function postJson(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

async function openYouTube(url) {
  resetStage();
  $('stageWrap').hidden = false;
  $('state').textContent = 'Hazırlanıyor…';
  try {
    st.job = await postJson('/api/jobs', { url });
  } catch (e) {
    $('state').textContent = `Hata: ${e.message}`;
    return;
  }
  await loadYouTubeApi();
  const host = document.createElement('div');
  $('stage').appendChild(host);
  const job = st.job;
  st.ytPlayer = new window.YT.Player(host, {
    videoId: job.source_id,
    playerVars: { playsinline: 1, rel: 0, cc_load_policy: 0 },
    events: {
      onReady: () => { if (st.job === job) { attachPlayer(ytAdapter(st.ytPlayer)); connectWs(); } },
      onError: (e) => {
        if (st.job !== job) return;
        // 101/150: sahibi gömmeyi kapatmış → yerel kopyayı indirip kendimiz oynatalım
        if (e.data === 101 || e.data === 150) playLocalCopy(job);
        else setNotice(`YouTube oynatıcı hatası (${e.data})`);
      },
    },
  });
}

function setNotice(text) {
  $('notice').textContent = text || '';
  $('notice').hidden = !text;
}

async function playLocalCopy(job) {
  setNotice('Bu videonun sahibi başka sitelerde oynatılmasını kapatmış. Yerel kopya indiriliyor…');
  if (st.ytPlayer) { st.ytPlayer.destroy(); st.ytPlayer = null; }
  if (st.player) { st.player.destroy(); st.player = null; }
  $('stage').innerHTML = '';
  let media;
  try {
    media = await postJson(`/api/youtube/${job.source_id}/media`, {});
  } catch (e) {
    if (st.job === job) setNotice(`Yerel kopya indirilemedi: ${e.message}. Chrome eklentisiyle YouTube üzerinden izleyin.`);
    return;
  }
  if (st.job !== job) return;
  setNotice('');
  const video = document.createElement('video');
  video.controls = true;
  video.src = media.url;
  $('stage').appendChild(video);
  attachPlayer(window.YouCev.videoAdapter(video));
  // Yeni oynatıcı tüm cümleleri baştan alsın
  if (st.ws) { const ws = st.ws; st.ws = null; ws.close(); }
  connectWs();
}

function uploadFile(file) {
  resetStage();
  $('stageWrap').hidden = false;
  $('title').textContent = file.name;
  $('state').textContent = 'Yükleniyor…';
  $('upload').hidden = false;
  const form = new FormData();
  form.append('file', file);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload');
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) $('uploadBar').style.width = `${(e.loaded / e.total) * 100}%`; };
  xhr.onload = () => {
    $('upload').hidden = true;
    if (xhr.status !== 200) { $('state').textContent = `Yükleme başarısız (${xhr.status})`; return; }
    st.job = JSON.parse(xhr.responseText);
    const video = document.createElement('video');
    video.controls = true;
    video.src = st.job.media_url;
    $('stage').appendChild(video);
    attachPlayer(window.YouCev.videoAdapter(video));
    connectWs();
  };
  xhr.onerror = () => { $('upload').hidden = true; $('state').textContent = 'Yükleme başarısız'; };
  xhr.send(form);
}

/* ---------- Ayarlar ---------- */

function bindSettings() {
  for (const id of ['duck', 'voiceVolume', 'subtitleSize']) {
    $(id).value = st.settings[id];
    $(id).addEventListener('input', () => {
      st.settings[id] = Number($(id).value);
      save();
      if (st.player) st.player.setOptions({ duck: st.settings.duck, voiceVolume: st.settings.voiceVolume });
      if (st.overlay) st.overlay.applySettings(st.settings);
    });
  }
  $('showEnglish').checked = st.settings.showEnglish;
  $('showEnglish').addEventListener('change', () => {
    st.settings.showEnglish = $('showEnglish').checked;
    save();
    if (st.player) st.player.setOptions({ showEnglish: st.settings.showEnglish });
  });
}

async function loadServer() {
  try {
    const [health, settings] = await Promise.all([fetch('/api/health').then((r) => r.json()), fetch('/api/settings').then((r) => r.json())]);
    document.querySelector('#health .dot').className = 'dot ok';
    const asr = health.asr.device ? `${health.asr.model} (${health.asr.device.toUpperCase()})` : health.asr.state;
    $('healthText').textContent = `Whisper: ${asr} · Çeviri: ${health.translator}`;
    const voice = $('voice');
    if (!voice.options.length) {
      for (const [id, name] of Object.entries(settings.voices)) voice.add(new Option(name, id));
    }
    if (document.activeElement !== voice) voice.value = settings.tts.voice;
    if (document.activeElement !== $('engine')) $('engine').value = settings.translate.engine;
  } catch (e) {
    document.querySelector('#health .dot').className = 'dot bad';
    $('healthText').textContent = 'Sunucuya ulaşılamıyor';
  }
}

async function changeServer(patch) {
  await postJson('/api/settings', patch);
  loadServer();
  // Ayar değişince aynı videoyu yeni ayarla yeniden aç
  if (st.job && st.job.kind === 'youtube' && st.player) {
    const t = st.player.media.currentTime;
    st.job = await postJson('/api/jobs', { url: st.job.source_id });
    if (st.ws) { const ws = st.ws; st.ws = null; ws.close(); }
    st.player.reset();
    connectWs();
    st.player.media.seek(t);
  }
}

$('voice').addEventListener('change', () => changeServer({ tts: { voice: $('voice').value } }));
$('engine').addEventListener('change', () => changeServer({ translate: { engine: $('engine').value } }));
$('urlForm').addEventListener('submit', (e) => { e.preventDefault(); openYouTube($('url').value.trim()); });
$('file').addEventListener('change', () => { if ($('file').files[0]) uploadFile($('file').files[0]); });

bindSettings();
loadServer();
setInterval(loadServer, 15000);

/* Arka plan: yerel sunucuyla tüm iletişim buradan geçer (YouTube sayfasının CSP ve
 * yerel ağ erişimi kısıtlamalarına takılmamak için). Her sekme bir "port" açar. */
'use strict';

const SERVER = 'http://127.0.0.1:8765';
const WS_SERVER = 'ws://127.0.0.1:8765';
const sessions = new Set();

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'youcev') return;
  const s = {
    port, ws: null, key: null, videoId: null, closed: false, retry: 0, lastPos: null, timer: null,
    tabId: port.sender && port.sender.tab ? port.sender.tab.id : null,
  };
  sessions.add(s);
  port.onMessage.addListener((m) => handle(s, m));
  port.onDisconnect.addListener(() => {
    s.closed = true;
    sessions.delete(s);
    closeWs(s);
    setBadge(s, '');
  });
});

// Eklenti simgesinde sekmenin durumu: hazırlık yüzdesi, ✓ (tamamlandı) veya ! (hata)
function setBadge(s, text, color) {
  if (s.tabId == null) return;
  try {
    chrome.action.setBadgeText({ tabId: s.tabId, text });
    if (color) chrome.action.setBadgeBackgroundColor({ tabId: s.tabId, color });
  } catch (e) { /* sekme kapanmış olabilir */ }
}

function badgeForProgress(s, p) {
  if (p.state === 'error') setBadge(s, '!', '#e5484d');
  else if (p.done) setBadge(s, '✓', '#2bb673');
  else setBadge(s, `${Math.floor(p.percent || 0)}%`, '#3d7eff');
}

// Popup'tan: ses/çeviri ayarı değişti → açık videoları yeni ayarla yeniden aç
chrome.runtime.onMessage.addListener((m) => {
  if (m && m.type === 'reopen') for (const s of sessions) if (s.videoId) openJob(s);
});

function post(s, msg) {
  if (s.closed) return;
  try { s.port.postMessage(msg); } catch (e) { s.closed = true; }
}

function handle(s, m) {
  if (m.type === 'open') {
    s.videoId = m.videoId;
    s.retry = 0;
    openJob(s);
  } else if (m.type === 'close') {
    s.videoId = null;
    closeWs(s);
    setBadge(s, '');
  } else if (m.type === 'position') {
    s.lastPos = m.t;
    if (s.ws && s.ws.readyState === WebSocket.OPEN) s.ws.send(JSON.stringify({ type: 'position', t: m.t }));
  } else if (m.type === 'clip') {
    fetchClip(s, m);
  }
}

function closeWs(s) {
  clearTimeout(s.timer);
  const ws = s.ws;
  s.ws = null;
  if (ws) { try { ws.close(); } catch (e) { /* yoksay */ } }
}

function scheduleRetry(s, vid) {
  clearTimeout(s.timer);
  const delay = Math.min(10000, 1000 * 2 ** s.retry++);
  s.timer = setTimeout(() => { if (!s.closed && s.videoId === vid) openJob(s); }, delay);
}

async function openJob(s) {
  closeWs(s);
  const vid = s.videoId;
  try {
    const r = await fetch(`${SERVER}/api/jobs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: `https://www.youtube.com/watch?v=${vid}` }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${r.status}`);
    }
    const job = await r.json();
    if (s.closed || s.videoId !== vid) return;
    s.key = job.key;
    post(s, { type: 'job', job });
    connectWs(s, vid);
  } catch (e) {
    if (s.closed || s.videoId !== vid) return;
    post(s, { type: 'status', ok: false, error: `You-Cev sunucusuna ulaşılamadı (${e.message}). start.bat çalışıyor mu?` });
    setBadge(s, '!', '#e5484d');
    scheduleRetry(s, vid);
  }
}

function connectWs(s, vid) {
  const ws = new WebSocket(`${WS_SERVER}/ws/${encodeURIComponent(s.key)}`);
  s.ws = ws;
  ws.onopen = () => {
    s.retry = 0;
    post(s, { type: 'status', ok: true });
    if (s.lastPos != null) ws.send(JSON.stringify({ type: 'position', t: s.lastPos }));
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === 'progress') badgeForProgress(s, msg);
    post(s, { type: 'ws', msg });
  };
  ws.onclose = () => {
    if (s.ws !== ws) return;
    s.ws = null;
    if (s.closed || s.videoId !== vid) return;
    post(s, { type: 'status', ok: false, error: 'You-Cev sunucusuyla bağlantı koptu, yeniden bağlanılıyor…' });
    scheduleRetry(s, vid);
  };
}

function toBase64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

async function fetchClip(s, m) {
  try {
    const r = await fetch(`${SERVER}/api/clips/${encodeURIComponent(m.key)}/${encodeURIComponent(m.segId)}.mp3`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    post(s, { type: 'clip', reqId: m.reqId, b64: toBase64(await r.arrayBuffer()) });
  } catch (e) {
    post(s, { type: 'clip', reqId: m.reqId, error: String(e.message || e) });
  }
}

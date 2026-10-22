'use strict';

const SERVER = 'http://127.0.0.1:8765';
const DEFAULTS = { enabled: true, duck: 0.15, voiceVolume: 1, subtitleSize: 26, showEnglish: false };
const $ = (id) => document.getElementById(id);
const pct = (v) => `${Math.round(v * 100)}%`;

function bindLocal(settings) {
  $('enabled').checked = settings.enabled;
  $('showEnglish').checked = settings.showEnglish;
  for (const [id, fmt] of [['duck', pct], ['voiceVolume', pct], ['subtitleSize', (v) => `${v}px`]]) {
    const input = $(id);
    input.value = settings[id];
    $(id + 'Val').textContent = fmt(settings[id]);
    input.addEventListener('input', () => {
      const v = Number(input.value);
      $(id + 'Val').textContent = fmt(v);
      chrome.storage.sync.set({ [id]: v });
    });
  }
  $('enabled').addEventListener('change', () => chrome.storage.sync.set({ enabled: $('enabled').checked }));
  $('showEnglish').addEventListener('change', () => chrome.storage.sync.set({ showEnglish: $('showEnglish').checked }));
}

async function saveServer(patch) {
  try {
    await fetch(`${SERVER}/api/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    chrome.runtime.sendMessage({ type: 'reopen' });
  } catch (e) {
    $('serverText').textContent = 'Ayar kaydedilemedi: sunucu kapalı';
  }
}

async function loadServer() {
  try {
    const [health, settings] = await Promise.all([
      fetch(`${SERVER}/api/health`).then((r) => r.json()),
      fetch(`${SERVER}/api/settings`).then((r) => r.json()),
    ]);
    $('dot').className = 'dot ok';
    const asr = health.asr.device ? `${health.asr.model} (${health.asr.device.toUpperCase()})` : health.asr.state;
    const local = health.local || {};
    const localNote = local.nllb && local.nllb !== 'hazır' && !String(local.nllb).startsWith('yüklü')
      ? ` · İnternetsiz çeviri yedeği: ${local.nllb}` : '';
    $('serverText').textContent = `Bağlı · Whisper: ${asr} · Çeviri: ${health.translator}${localNote}`;
    const voice = $('voice');
    for (const [id, name] of Object.entries(settings.voices)) voice.add(new Option(name, id));
    voice.value = settings.tts.voice;
    if ((health.voices_down || []).includes(settings.tts.voice)) {
      $('voiceWarn').textContent = 'Bu ses şu an Microsoft tarafında çalışmıyor; geçici olarak diğer Türkçe ses kullanılıyor.';
      $('voiceWarn').hidden = false;
    }
    $('engine').value = settings.translate.engine;
    voice.addEventListener('change', () => saveServer({ tts: { voice: voice.value } }));
    $('engine').addEventListener('change', () => saveServer({ translate: { engine: $('engine').value } }));
  } catch (e) {
    $('dot').className = 'dot bad';
    $('serverText').textContent = 'Sunucu kapalı — start.bat dosyasını çalıştırın';
    $('voice').disabled = $('engine').disabled = true;
  }
}

chrome.storage.sync.get(DEFAULTS, (s) => bindLocal({ ...DEFAULTS, ...s }));
loadServer();

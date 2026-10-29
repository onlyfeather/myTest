// 一起听前端。Vue 3（本地 ESM 构建，不用构建工具）。
// 用户内容（歌名、昵称、聊天）一律用 {{ }} 文本插值，不用 v-html。
import { createApp, reactive, computed, watch, nextTick } from '/static/vendor/vue.esm-browser.prod.js';

// 纠偏阈值，和 sync.py 保持一致。
const IGNORE_MS = 300;
const JUMP_MS = 2000;

const s = reactive({
  view: 'loading', // loading | code | message | main
  message: { title: '', detail: '', retry: false },
  notice: '',
  code: '', codeExpires: 0, codeError: '',
  me: null,
  state: null, queue: [], members: [], chat: [],
  connected: false, unlocked: false,
  tick: 0, dragging: false, dragValue: 0,
  q: '', results: [], searching: false, searchError: '', vipReady: false,
  platforms: [], platform: 'qq',
  lyrics: [], lyricsState: '', showLyrics: false,
  profile: null, profileLib: null, profileTab: 'favorites', playlistView: null,
  liked: new Set(),
  lib: null, libPlatform: '', uid: '', libBusy: false, libError: '',
  qr: null, importList: null, importVia: '', importPicked: {},
  chatText: '', toast: '', tab: 'now',
});

// ---------------------------------------------------------------- HTTP

async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, credentials: 'same-origin', headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    return { ok: false, status: 0, message: '网络连接失败' };
  }
  let data = {};
  try { data = await res.json(); } catch { /* 非 JSON */ }
  return { status: res.status, ...data };
}

let toastTimer = 0;
function toast(text) {
  s.toast = text;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { s.toast = ''; }, 3000);
}

function showMessage(title, detail = '', retry = false) {
  s.view = 'message';
  s.message = { title, detail, retry };
}

// ---------------------------------------------------------------- 登录

// 模块加载时就把 token 取出来并从地址栏去掉：不留在浏览器历史里，
// 也避免 boot() 被再次调用（重新进入、重连）时拿同一个一次性链接重复登录。
const linkToken = (() => {
  const m = location.hash.match(/[#&]t=([^&]+)/);
  if (!m) return '';
  history.replaceState(null, '', '/');
  return decodeURIComponent(m[1]);
})();
let linkUsed = false;

async function boot() {
  s.view = 'loading';
  if (linkToken && !linkUsed) {
    linkUsed = true;
    const r = await api('/api/login/link', { method: 'POST', body: { token: linkToken } });
    // 链接失效但浏览器里已经登录过时不提示，直接进。
    if (!r.ok) s.notice = r.message || '登录失败';
  }
  const me = await api('/api/me');
  if (me.status === 401) return startCode();
  if (!me.ok) return showMessage(me.message || '出错了', '', true);
  s.me = me;
  s.notice = ''; // 已经登录了，链接失效的提示没有意义
  loadPlatforms();
  loadLiked();
  connect();
}

let codeTimer = 0;
async function startCode(renew = false) {
  s.view = 'code';
  clearTimeout(codeTimer);
  const r = await api('/api/login/code', { method: 'POST', body: { renew } });
  if (r.done) return boot();
  showCode(r);
  codeTimer = setTimeout(pollCode, 2000);
}

function showCode(r) {
  if (r.code) {
    s.code = r.code;
    s.codeExpires = Date.now() + (r.expires_in_ms || 0);
    s.codeError = '';
  } else if (!r.ok) {
    s.codeError = r.message || '拿不到验证码';
  }
}

async function pollCode() {
  if (s.view !== 'code') return;
  // 码过期后服务端会自动发新的，这里不用主动要。
  const r = await api('/api/login/code', { method: 'POST', body: {} });
  if (r.done) return boot();
  showCode(r);
  codeTimer = setTimeout(pollCode, 2000);
}

async function logout() {
  await api('/api/logout', { method: 'POST', body: {} });
  if (ws) ws.close();
  location.replace('/');
}

// ---------------------------------------------------------------- 时钟

let offset = 0; // 服务端时间 - 本地时间
let samples = [];
const serverNow = () => Date.now() + offset;

function ping() { send({ type: 'ping', client_ms: Date.now() }); }

function calibrate() {
  samples = [];
  for (let i = 0; i < 5; i++) setTimeout(ping, i * 260);
}

function onPong(msg) {
  const now = Date.now();
  const rtt = now - msg.client_ms;
  if (rtt < 0 || rtt > 10000) return;
  samples.push({ rtt, offset: msg.server_ms - (msg.client_ms + rtt / 2) });
  if (samples.length > 10) samples.shift();
  // 往返最短的那次最准（和 NTP 一样）。
  offset = samples.reduce((a, b) => (b.rtt < a.rtt ? b : a)).offset;
}

// ---------------------------------------------------------------- 播放

const audio = new Audio();
window.__listenAudio = audio; // 方便在控制台排查播放问题
audio.preload = 'auto';
audio.referrerPolicy = 'no-referrer';
audio.crossOrigin = null;
let loadedRef = null;
let loadSeq = 0;
let retries = 0;

function expectedMs() {
  const st = s.state;
  if (!st || !st.current) return 0;
  if (st.paused) return st.anchor_position_ms;
  return st.anchor_position_ms + Math.max(0, serverNow() - st.anchor_server_ms);
}

async function loadTrack(track) {
  const seq = ++loadSeq;
  if (!track) {
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    return;
  }
  const r = await api('/api/stream?ref=' + encodeURIComponent(track.ref));
  if (seq !== loadSeq) return;
  if (!r.ok) {
    // 服务端会跳过放不了的歌并广播新状态，这里只提示。
    toast(r.message || '这首放不了');
    return;
  }
  audio.src = r.url;
  audio.playbackRate = 1;
}

async function applyState() {
  const st = s.state;
  const track = st && st.current ? st.current.track : null;
  const ref = track ? track.ref : null;
  if (ref !== loadedRef) {
    loadedRef = ref;
    retries = 0;
    await loadTrack(track);
  }
  updateMediaSession(track);
  loadLyrics(track);
  syncAudio();
}

function syncAudio() {
  const st = s.state;
  if (!st || !st.current || !audio.src || audio.readyState < 1) return;
  const expected = expectedMs();
  const local = audio.currentTime * 1000;
  if (st.paused) {
    if (!audio.paused) audio.pause();
    if (Math.abs(local - expected) > IGNORE_MS) audio.currentTime = expected / 1000;
    audio.playbackRate = 1;
    return;
  }
  if (!s.unlocked || localPaused) return;
  const duration = st.current.track.duration_ms;
  if (duration && expected >= duration) return; // 等服务端切下一首
  if (audio.paused) {
    audio.currentTime = expected / 1000;
    audio.play().catch((e) => { if (e.name === 'NotAllowedError') s.unlocked = false; });
    return;
  }
  const drift = local - expected;
  const abs = Math.abs(drift);
  if (abs < IGNORE_MS) audio.playbackRate = 1;
  else if (abs < JUMP_MS) audio.playbackRate = drift > 0 ? 0.95 : 1.05;
  else { audio.currentTime = expected / 1000; audio.playbackRate = 1; }
}

audio.addEventListener('loadedmetadata', syncAudio);
audio.addEventListener('error', () => {
  // 地址过期或者网络问题：重新取地址，最多 3 次。
  if (!loadedRef || retries >= 3) return;
  retries += 1;
  const ref = loadedRef;
  setTimeout(() => {
    if (loadedRef !== ref) return;
    loadedRef = null;
    applyState();
  }, 1000 * retries);
});

// 一段很短的静音，点「开始收听」时在用户手势里播一下，解锁 iOS 上的 <audio>。
function silentWav() {
  const n = 800;
  const buf = new ArrayBuffer(44 + n);
  const v = new DataView(buf);
  const str = (o, t) => { for (let i = 0; i < t.length; i++) v.setUint8(o + i, t.charCodeAt(i)); };
  str(0, 'RIFF'); v.setUint32(4, 36 + n, true); str(8, 'WAVE'); str(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, 8000, true); v.setUint32(28, 8000, true); v.setUint16(32, 1, true); v.setUint16(34, 8, true);
  str(36, 'data'); v.setUint32(40, n, true);
  for (let i = 0; i < n; i++) v.setUint8(44 + i, 128);
  return URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
}

async function unlock() {
  s.unlocked = true;
  localPaused = false;
  if (!audio.src) {
    audio.src = silentWav();
    try { await audio.play(); } catch { /* 忽略 */ }
    audio.pause();
    audio.removeAttribute('src');
    loadedRef = null;
    await applyState();
  } else {
    audio.play().catch(() => {});
    syncAudio();
  }
}

function updateMediaSession(track) {
  if (!('mediaSession' in navigator)) return;
  if (!track) { navigator.mediaSession.metadata = null; return; }
  navigator.mediaSession.metadata = new MediaMetadata({
    title: track.title, artist: track.artists, album: track.album,
    artwork: track.cover ? [{ src: track.cover, sizes: '300x300', type: 'image/jpeg' }] : [],
  });
}

// 锁屏、耳机按钮、系统媒体键会调用这些处理函数。它们只影响自己：暂停时自己不听了，
// 不替大家暂停（拔耳机、来电时系统也会发 pause，不能因此让全房间停下）。
// 切歌是明确的操作，照常发给服务端。
let localPaused = false;

if ('mediaSession' in navigator) {
  navigator.mediaSession.setActionHandler('play', () => { localPaused = false; s.unlocked = true; syncAudio(); });
  navigator.mediaSession.setActionHandler('pause', () => { localPaused = true; audio.pause(); });
  navigator.mediaSession.setActionHandler('nexttrack', () => control('next'));
}

// ---------------------------------------------------------------- WebSocket

let ws = null;
let reconnectDelay = 1000;
let reconnectTimer = 0;
let pingTimer = 0;

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function connect() {
  s.view = 'main';
  clearTimeout(reconnectTimer);
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    s.connected = true;
    reconnectDelay = 1000;
    calibrate();
    clearInterval(pingTimer);
    pingTimer = setInterval(ping, 30000);
  };
  ws.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    onMessage(msg);
  };
  ws.onclose = (e) => {
    s.connected = false;
    clearInterval(pingTimer);
    onClose(e.code);
  };
}

let closedMessage = '';

function onClose(code) {
  if (code === 4401) return boot();
  if (code === 4403) return showMessage('进不去', '你不在这个群里，或者这个群没有开放一起听。');
  if (code === 4029) return showMessage('房间满了', closedMessage, true);
  if (code === 4000 || code === 4004 || code === 4003) {
    audio.pause();
    return showMessage(closedMessage || '现在没有一起听', '在群里发 /一起听 开一个，然后点「重新进入」。', true);
  }
  reconnectTimer = setTimeout(connect, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, 30000);
}

function onMessage(msg) {
  switch (msg.type) {
    case 'hello':
      s.me = { ...(s.me || {}), qq: msg.qq, name: msg.name, superuser: msg.superuser };
      break;
    case 'state':
      if (!samples.length) offset = msg.state.server_ms - Date.now();
      s.state = msg.state;
      applyState();
      break;
    case 'queue': s.queue = msg.queue; break;
    case 'members': s.members = msg.members; break;
    case 'chat':
      s.chat.push(msg);
      if (s.chat.length > 200) s.chat.splice(0, s.chat.length - 200);
      setTimeout(() => {
        const el = document.querySelector('.chat-scroll');
        if (el) el.scrollTop = el.scrollHeight;
      });
      break;
    case 'pong': onPong(msg); break;
    case 'error': toast(msg.message); break;
    case 'closed': closedMessage = msg.message || ''; break;
    default: break;
  }
}

// ---------------------------------------------------------------- 操作

function control(action, extra = {}) {
  send({ type: 'control', action, version: s.state ? s.state.version : null, ...extra });
}

async function loadPlatforms() {
  const r = await api('/api/platforms');
  if (!r.ok) return;
  s.platforms = r.platforms;
  if (!s.platforms.some((p) => p.id === s.platform)) s.platform = (s.platforms[0] || {}).id || 'qq';
}

let searchSeq = 0;
async function search() {
  const q = s.q.trim();
  if (!q) return;
  const seq = ++searchSeq;
  s.searching = true;
  s.searchError = '';
  const r = await api(`/api/search?platform=${encodeURIComponent(s.platform)}&q=${encodeURIComponent(q)}`);
  if (seq !== searchSeq) return; // 切了平台或者又搜了一次
  s.searching = false;
  if (!r.ok) { s.results = []; s.searchError = r.message || '搜索失败'; return; }
  s.results = r.tracks;
  s.vipReady = !!r.vip_ready;
  if (!r.tracks.length) s.searchError = '没搜到';
}

function pickPlatform(id) {
  if (s.platform === id) return;
  s.platform = id;
  s.results = [];
  if (s.q.trim()) search();
}

function addTrack(track) {
  send({ type: 'queue', op: 'add', ref: track.ref });
  toast(track.vip && !s.vipReady ? `在别的平台找《${track.title}》…` : `点了 ${track.title}`);
}

// ---------------------------------------------------------------- 歌词

// 解析 LRC：[mm:ss.xx] 文本，一行可以有多个时间标签。翻译按时间对上原文。
function parseLrc(text) {
  const lines = [];
  for (const raw of (text || '').split(/\r?\n/)) {
    const tags = [...raw.matchAll(/\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]/g)];
    if (!tags.length) continue;
    const content = raw.replace(/\[[^\]]*\]/g, '').trim();
    for (const t of tags) {
      const frac = t[3] ? Number(t[3].padEnd(3, '0')) : 0;
      lines.push({ t: Number(t[1]) * 60000 + Number(t[2]) * 1000 + frac, text: content });
    }
  }
  return lines.sort((a, b) => a.t - b.t);
}

function mergeLyrics(lrc, trans) {
  const main = parseLrc(lrc).filter((l) => l.text);
  const tr = new Map(parseLrc(trans).filter((l) => l.text).map((l) => [l.t, l.text]));
  return main.map((l) => ({ ...l, trans: tr.get(l.t) || '' }));
}

let lyricsRef = null;
async function loadLyrics(track) {
  const ref = track ? track.ref : null;
  if (ref === lyricsRef) return;
  lyricsRef = ref;
  s.lyrics = [];
  s.lyricsState = ref ? 'loading' : '';
  if (!ref) return;
  const r = await api('/api/lyrics?ref=' + encodeURIComponent(ref));
  if (lyricsRef !== ref) return;
  if (!r.ok) { s.lyricsState = 'error'; return; }
  s.lyrics = mergeLyrics(r.lrc, r.trans);
  s.lyricsState = s.lyrics.length ? 'ok' : 'none';
}

let lastLyricIndex = -1;
function scrollLyric(index) {
  if (index === lastLyricIndex) return;
  lastLyricIndex = index;
  const box = document.querySelector('.lyrics');
  const el = box && box.querySelector(`[data-i="${index}"]`);
  // offsetTop 是相对最近的定位祖先算的，不一定是 .lyrics；用两者的位置差算才准。
  if (el) {
    const offset = el.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop;
    box.scrollTo({ top: offset - box.clientHeight / 2 + el.offsetHeight / 2, behavior: 'smooth' });
  }
}

// ---------------------------------------------------------------- 资料卡

async function openProfile(qq) {
  s.profile = { qq, loading: true };
  s.profileLib = null;
  s.profileTab = 'favorites';
  s.playlistView = null;
  const [r, lib] = await Promise.all([
    api('/api/members/' + encodeURIComponent(qq)),
    api('/api/library/' + encodeURIComponent(qq)),
  ]);
  if (!s.profile || s.profile.qq !== qq) return;
  s.profile = r.ok ? r : { qq, error: r.message || '打不开资料卡' };
  s.profileLib = lib.ok ? lib : null;
}

async function openPlaylist(p) {
  s.playlistView = { ...p, loading: true, tracks: [] };
  const r = await api('/api/playlists/' + encodeURIComponent(p.id));
  if (!s.playlistView || s.playlistView.id !== p.id) return;
  s.playlistView = r.ok ? r : { ...p, error: r.message || '打不开歌单', tracks: [] };
}

// ---------------------------------------------------------------- 喜欢的音乐

async function loadLiked() {
  const r = await api('/api/favorites/refs');
  if (r.ok) s.liked = new Set(r.refs);
}

async function toggleLike(track) {
  const on = s.liked.has(track.ref);
  const r = await api(on ? '/api/favorites/remove' : '/api/favorites', { method: 'POST', body: { ref: track.ref } });
  if (!r.ok) { toast(r.message || '没成功'); return; }
  const next = new Set(s.liked);
  if (on) next.delete(track.ref); else next.add(track.ref);
  s.liked = next;
  toast(on ? '从喜欢的音乐里拿掉了' : '加到喜欢的音乐了');
  if (s.profileLib && s.profileLib.mine) openProfile(s.profile.qq);
}

// ---------------------------------------------------------------- 我的曲库：账号、扫码、导入

async function loadLib() {
  const r = await api('/api/library/accounts');
  if (!r.ok) { s.libError = r.message || '读不到'; return; }
  s.lib = r;
  if (!s.libPlatform && r.platforms.length) s.libPlatform = r.platforms[0].id;
}

const libPlatformInfo = () => (s.lib ? s.lib.platforms.find((p) => p.id === s.libPlatform) : null);
const libAccount = () => (s.lib ? s.lib.accounts.find((a) => a.platform === s.libPlatform) : null);

let qrTimer = 0;
async function startQr(kind) {
  clearTimeout(qrTimer);
  s.libError = '';
  s.importList = null;
  s.qr = { loading: true, kind };
  const r = await api('/api/library/qr', { method: 'POST', body: { platform: s.libPlatform, kind } });
  if (!r.ok) { s.qr = null; s.libError = r.message || '二维码获取失败'; return; }
  s.qr = { sid: r.sid, image: r.image, hint: r.hint, status: 'waiting', kind, expires: Date.now() + 5 * 60 * 1000 };
  qrTimer = setTimeout(pollQr, 2000);
}

async function pollQr() {
  const qr = s.qr;
  if (!qr || !qr.sid) return;
  if (Date.now() > qr.expires) { s.qr = { ...qr, status: 'expired' }; return; }
  const r = await api('/api/library/qr/' + encodeURIComponent(qr.sid));
  if (s.qr !== qr) return;
  if (!r.ok) { s.qr = null; s.libError = r.message || '登录失败'; return; }
  if (r.status === 'done') {
    s.qr = null;
    toast(`登录好了${r.account ? '：' + r.account : ''}`);
    await loadLib();
    showImport(r.playlists || [], 'login');
    if (r.message) s.libError = r.message;
    return;
  }
  s.qr = { ...qr, status: r.status };
  if (r.status === 'expired' || r.status === 'refused') return;
  qrTimer = setTimeout(pollQr, 2000);
}

function cancelQr() { clearTimeout(qrTimer); s.qr = null; }

function showImport(list, via) {
  s.importList = list;
  s.importVia = via;
  // 默认勾上“我喜欢”和所有公开歌单；私密歌单默认不勾（导入后也是仅自己可见）
  s.importPicked = Object.fromEntries(list.map((p) => [p.remote_id, !p.private]));
}

async function lookupUid() {
  const uid = s.uid.trim();
  if (!uid) return;
  s.libBusy = true;
  s.libError = '';
  cancelQr();
  const r = await api('/api/library/uid', { method: 'POST', body: { platform: s.libPlatform, account_id: uid } });
  s.libBusy = false;
  if (!r.ok) { s.libError = r.message || '查不到'; return; }
  if (!r.playlists.length) { s.libError = '这个账号没有公开歌单'; return; }
  showImport(r.playlists, 'uid');
}

async function doImport() {
  const ids = Object.keys(s.importPicked).filter((k) => s.importPicked[k]);
  if (!ids.length) { s.libError = '先勾选要导入的歌单'; return; }
  s.libBusy = true;
  s.libError = '';
  const r = await api('/api/library/import', { method: 'POST', body: { platform: s.libPlatform, via: s.importVia, remote_ids: ids } });
  s.libBusy = false;
  if (!r.ok) { s.libError = r.message || '导入失败'; return; }
  const total = r.imported.reduce((n, p) => n + p.tracks, 0);
  toast(`导入了 ${r.imported.length} 个歌单，共 ${total} 首`);
  s.importList = null;
  if (s.me) openProfile(s.me.qq);
}

async function unbind() {
  const acc = libAccount();
  if (!acc || !confirm(`解绑${acc.name}吗？登录信息会马上删掉。已经导入的歌单会留着，但私密歌单以后不会再同步。`)) return;
  const r = await api(`/api/library/accounts/${encodeURIComponent(s.libPlatform)}/logout`, { method: 'POST', body: {} });
  if (r.ok) { toast('解绑了'); loadLib(); }
}

async function setHidden(p, hidden) {
  const r = await api(`/api/playlists/${encodeURIComponent(p.id)}/visibility`, { method: 'POST', body: { hidden } });
  if (r.ok) { p.hidden = hidden; toast(hidden ? '这个歌单现在只有你自己看得到' : '这个歌单群友也能看到了'); }
}

async function removePlaylist(p) {
  if (!confirm(`从一起听里删掉「${p.name}」吗？平台上的歌单不受影响。`)) return;
  const r = await api(`/api/playlists/${encodeURIComponent(p.id)}/delete`, { method: 'POST', body: {} });
  if (r.ok) { toast('删掉了'); if (s.me) openProfile(s.me.qq); }
}

function sendChat() {
  const text = s.chatText.trim();
  if (!text) return;
  send({ type: 'chat', text: text.slice(0, 200) });
  s.chatText = '';
}

setInterval(() => { s.tick += 1; }, 250);
setInterval(syncAudio, 1000);

function fmt(ms) {
  const t = Math.max(0, Math.floor((ms || 0) / 1000));
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`;
}

// ---------------------------------------------------------------- 界面

const App = {
  setup() {
    const track = computed(() => (s.state && s.state.current ? s.state.current.track : null));
    const position = computed(() => {
      void s.tick;
      if (s.dragging) return s.dragValue;
      const p = expectedMs();
      return track.value && track.value.duration_ms ? Math.min(p, track.value.duration_ms) : p;
    });
    const canControl = computed(() => {
      const st = s.state;
      if (!st || !s.me) return false;
      return st.allow_control || s.me.superuser || s.me.qq === st.owner;
    });
    const isOwner = computed(() => !!(s.state && s.me && (s.me.qq === s.state.owner || s.me.superuser)));
    const codeLeft = computed(() => { void s.tick; return Math.max(0, s.codeExpires - Date.now()); });
    const mine = (item) => s.me && item.added_by === s.me.qq;
    const lyricIndex = computed(() => {
      const p = position.value;
      let lo = 0; let hi = s.lyrics.length - 1; let found = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (s.lyrics[mid].t <= p + 200) { found = mid; lo = mid + 1; } else hi = mid - 1;
      }
      return found;
    });
    const currentLine = computed(() => (lyricIndex.value >= 0 ? s.lyrics[lyricIndex.value].text : ''));
    // flush: 'post'：等 Vue 把新的 .on 渲染到 DOM 之后再量位置。
    watch(lyricIndex, (i) => { if (s.showLyrics) scrollLyric(i); }, { flush: 'post' });
    watch(() => s.showLyrics, (on) => { if (on) { lastLyricIndex = -1; nextTick(() => scrollLyric(lyricIndex.value)); } });

    return {
      s, track, position, canControl, isOwner, codeLeft, fmt, mine,
      lyricIndex, currentLine, openProfile, pickPlatform,
      toggleLike, openPlaylist, loadLib, startQr, cancelQr, lookupUid, doImport, unbind, setHidden, removePlaylist,
      libPlatformInfo, libAccount,
      openMine() { s.tab = 'mine'; if (!s.lib) loadLib(); if (s.me) openProfile(s.me.qq); },
      pickLibPlatform(id) { s.libPlatform = id; s.importList = null; s.libError = ''; s.uid = ''; cancelQr(); },
      when(ms) { if (!ms) return ''; const d = new Date(ms); return `${d.getMonth() + 1}月${d.getDate()}日 ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; },
      platformName(id) { const p = s.platforms.find((x) => x.id === id); return p ? p.name : id; },
      boot, startCode, logout, unlock, control, search, addTrack, sendChat,
      resume() { localPaused = false; control('play'); },
      seekInput(e) { s.dragging = true; s.dragValue = Number(e.target.value); },
      seekCommit(e) { s.dragging = false; control('seek', { position_ms: Number(e.target.value) }); },
      remove(item) { send({ type: 'queue', op: 'remove', item_id: item.item_id }); },
      moveUp(item, i) { if (i > 0) send({ type: 'queue', op: 'move', item_id: item.item_id, to: i - 1 }); },
      toggleControl(e) { send({ type: 'setting', allow_control: e.target.checked }); },
      avatar(qq) { return '/avatar/' + encodeURIComponent(qq); },
    };
  },
  template: `
<div v-if="s.view === 'loading'" class="center"><div class="muted">加载中…</div></div>

<div v-else-if="s.view === 'message'" class="center">
  <div class="card">
    <h1>{{ s.message.title }}</h1>
    <p class="muted" v-if="s.message.detail">{{ s.message.detail }}</p>
    <button v-if="s.message.retry" class="primary" @click="boot()">重新进入</button>
  </div>
</div>

<div v-else-if="s.view === 'code'" class="center">
  <div class="card">
    <h1>一起听</h1>
    <p v-if="s.notice" class="muted">{{ s.notice }}</p>
    <p class="muted">在群里发 <b>/一起听</b>，凛音会私聊你登录链接。<br>私聊收不到的话，把下面的验证码发给凛音（群里或私聊都行）：</p>
    <div class="code" v-if="s.code">{{ s.code }}</div>
    <div class="cmd" v-if="s.code">/一起听 登录 {{ s.code }}</div>
    <p class="muted small" v-if="s.code">{{ Math.ceil(codeLeft / 60000) }} 分钟内有效</p>
    <p class="small error" v-if="s.codeError">{{ s.codeError }}</p>
    <button @click="startCode(true)">换一个</button>
  </div>
</div>

<template v-else>
  <div class="banner" v-if="!s.connected">连接断开，正在重连…</div>
  <div class="layout">
    <section class="panel a-search" :class="{ active: s.tab === 'search' }">
      <header>点歌</header>
      <div class="body">
        <div class="seg" v-if="s.platforms.length > 1">
          <button v-for="p in s.platforms" :key="p.id" :class="{ on: s.platform === p.id }"
                  :disabled="!p.available" :title="p.available ? '' : '暂时不可用'" @click="pickPlatform(p.id)">{{ p.name }}</button>
        </div>
        <form class="row" @submit.prevent="search()">
          <input v-model="s.q" placeholder="歌名、歌手" maxlength="60" enterkeyhint="search">
          <button class="primary" :disabled="s.searching">搜索</button>
        </form>
        <p class="muted small" v-if="s.searching">搜索中…</p>
        <p class="muted small" v-if="s.searchError">{{ s.searchError }}</p>
        <ul class="list">
          <li v-for="t in s.results" :key="t.ref">
            <img class="thumb" v-if="t.cover" :src="t.cover" loading="lazy" alt="">
            <div class="grow">
              <div class="ellipsis">{{ t.title }} <span class="tag vip" v-if="t.vip">会员</span></div>
              <div class="muted small ellipsis">{{ t.artists }} · {{ fmt(t.duration_ms) }}</div>
            </div>
            <button class="icon like" :class="{ on: s.liked.has(t.ref) }" @click="toggleLike(t)"
                    :title="s.liked.has(t.ref) ? '从喜欢的音乐里拿掉' : '加到喜欢的音乐'">{{ s.liked.has(t.ref) ? '♥' : '♡' }}</button>
            <button class="icon" @click="addTrack(t)"
                    :title="t.vip && !s.vipReady ? '会员歌：会到别的平台找同一首' : '点歌'">＋</button>
          </li>
        </ul>
      </div>
    </section>

    <section class="panel now a-now" :class="{ active: s.tab === 'now' }">
      <div class="stage" @click="track && (s.showLyrics = !s.showLyrics)" :title="track ? '点一下切换封面和歌词' : ''">
        <div v-if="track && s.showLyrics" class="lyrics">
          <p class="muted" v-if="s.lyricsState === 'loading'">歌词加载中…</p>
          <p class="muted" v-else-if="s.lyricsState === 'none'">这首没有歌词</p>
          <p class="muted" v-else-if="s.lyricsState === 'error'">歌词获取失败</p>
          <div v-for="(l, i) in s.lyrics" :key="i" :data-i="i" class="line" :class="{ on: i === lyricIndex }">
            <div>{{ l.text }}</div><div class="trans" v-if="l.trans">{{ l.trans }}</div>
          </div>
        </div>
        <img v-else-if="track && track.cover" class="cover" :src="track.cover" alt="">
        <div v-else class="cover empty">{{ track ? '没有封面' : '还没有在放歌' }}</div>
      </div>
      <div v-if="track">
        <div class="title">{{ track.title }}</div>
        <div class="muted">{{ track.artists }}<span v-if="track.album"> · {{ track.album }}</span></div>
        <div class="muted small">
          <span class="tag">{{ platformName(track.platform) }}</span>
          <template v-if="s.state.current.added_by_name"> · <a class="link" @click="openProfile(s.state.current.added_by)">{{ s.state.current.added_by_name }}</a> 点的</template>
        </div>
        <button class="icon like big-like" :class="{ on: s.liked.has(track.ref) }" @click="toggleLike(track)"
                :title="s.liked.has(track.ref) ? '从喜欢的音乐里拿掉' : '加到喜欢的音乐'">{{ s.liked.has(track.ref) ? '♥ 喜欢' : '♡ 喜欢' }}</button>
        <div class="muted small current-line" v-if="!s.showLyrics && currentLine">{{ currentLine }}</div>
      </div>
      <div class="progress" v-if="track">
        <input type="range" min="0" :max="track.duration_ms || 1" step="1000" :value="position"
               :disabled="!canControl" @input="seekInput" @change="seekCommit">
        <div class="row muted small"><span>{{ fmt(position) }}</span><span class="grow"></span><span>{{ fmt(track.duration_ms) }}</span></div>
      </div>
      <div class="controls">
        <button v-if="!s.unlocked && track" class="primary" @click="unlock()">开始收听</button>
        <template v-else>
          <button v-if="!s.state || s.state.paused" :disabled="!canControl || (!track && !s.queue.length)" @click="resume()">播放</button>
          <button v-else :disabled="!canControl" @click="control('pause')">暂停</button>
          <button :disabled="!canControl || (!track && !s.queue.length)" @click="control('next')">下一首</button>
        </template>
      </div>
      <p class="muted small" v-if="s.state && !canControl">房主关掉了大家控制播放的权限</p>
      <label class="row small muted" v-if="isOwner">
        <input type="checkbox" class="check" :checked="s.state.allow_control" @change="toggleControl"> 允许大家切歌、暂停、拖进度
      </label>
    </section>

    <section class="panel a-queue" :class="{ active: s.tab === 'queue' }">
      <header>接下来 <span class="muted small">{{ s.queue.length }} 首</span></header>
      <div class="body">
        <p class="muted small" v-if="!s.queue.length">队列是空的，去点首歌吧</p>
        <ul class="list">
          <li v-for="(item, i) in s.queue" :key="item.item_id">
            <img class="thumb" v-if="item.track.cover" :src="item.track.cover" loading="lazy" alt="">
            <div class="grow">
              <div class="ellipsis">{{ item.track.title }}</div>
              <div class="muted small ellipsis">{{ item.track.artists }} · {{ platformName(item.track.platform) }} · {{ item.added_by_name }} 点的</div>
            </div>
            <button class="icon" v-if="canControl && i > 0" @click="moveUp(item, i)" title="往前挪">↑</button>
            <button class="icon" v-if="canControl || mine(item)" @click="remove(item)" title="删掉">✕</button>
          </li>
        </ul>
      </div>
    </section>

    <section class="panel a-chat" :class="{ active: s.tab === 'chat' }">
      <header>
        <span>在听 {{ s.members.length }} 人</span><span class="grow"></span>
        <span class="muted small ellipsis">{{ s.me && s.me.name }}</span>
        <button class="icon small" @click="openMine()" title="我的喜欢的音乐、歌单和导入">我的</button>
        <button class="icon small" @click="logout()">退出</button>
      </header>
      <div class="body members">
        <div class="row wrap">
          <button class="member" v-for="m in s.members" :key="m.qq" :title="m.title" @click="openProfile(m.qq)">
            <img class="avatar" :src="avatar(m.qq)" loading="lazy" alt="">{{ m.name }}
          </button>
        </div>
      </div>
      <div class="body chat chat-scroll">
        <div v-for="(c, i) in s.chat" :key="i" class="msg" :class="{ system: c.system }">
          <template v-if="c.system">{{ c.text }}</template>
          <template v-else><a class="who" @click="openProfile(c.from)">{{ c.name }}</a>{{ c.text }}</template>
        </div>
      </div>
      <form class="chat-input row" @submit.prevent="sendChat()">
        <input v-model="s.chatText" maxlength="200" placeholder="说点什么" enterkeyhint="send">
        <button>发送</button>
      </form>
    </section>
  </div>

  <div class="mini" v-if="track && s.tab !== 'now'" @click="s.tab = 'now'">
    <div class="bar" :style="{ width: (track.duration_ms ? position / track.duration_ms * 100 : 0) + '%' }"></div>
    <img class="thumb" v-if="track.cover" :src="track.cover" alt="">
    <div class="grow"><div class="ellipsis">{{ track.title }}</div><div class="muted small ellipsis">{{ track.artists }}</div></div>
    <button v-if="!s.unlocked" class="primary" @click.stop="unlock()">收听</button>
  </div>
  <nav class="tabs">
    <button :class="{ on: s.tab === 'now' }" @click="s.tab = 'now'">播放</button>
    <button :class="{ on: s.tab === 'queue' }" @click="s.tab = 'queue'">队列</button>
    <button :class="{ on: s.tab === 'search' }" @click="s.tab = 'search'">点歌</button>
    <button :class="{ on: s.tab === 'chat' }" @click="s.tab = 'chat'">聊天</button>
    <button @click="openMine()">我的</button>
  </nav>
</template>

<div class="modal" v-if="s.profile" @click.self="s.profile = null">
  <div class="card profile">
    <button class="icon close" @click="s.profile = null" title="关闭">✕</button>
    <img class="avatar big" :src="avatar(s.profile.qq)" alt="">
    <p class="muted" v-if="s.profile.loading">加载中…</p>
    <p class="error" v-else-if="s.profile.error">{{ s.profile.error }}</p>
    <template v-else>
      <h2>{{ s.profile.name }}</h2>
      <div class="muted small" v-if="s.profile.card && s.profile.nickname && s.profile.card !== s.profile.nickname">{{ s.profile.nickname }}</div>
      <div class="row center-row">
        <span class="tag" v-if="s.profile.role === 'owner'">群主</span>
        <span class="tag" v-else-if="s.profile.role === 'admin'">管理员</span>
        <span class="tag" v-if="s.profile.title">{{ s.profile.title }}</span>
        <span class="tag" v-if="s.profile.group_level">群等级 {{ s.profile.group_level }}</span>
        <span class="tag" v-if="s.profile.qq_level">QQ 等级 {{ s.profile.qq_level }}</span>
      </div>
      <p class="signature" v-if="s.profile.signature">{{ s.profile.signature }}</p>
      <p class="muted small">
        {{ s.profile.online ? '正在一起听' : '不在房间里' }}<span v-if="s.profile.playing_theirs"> · 现在放的是 TA 点的</span><span v-if="s.profile.in_queue"> · 接下来还有 {{ s.profile.in_queue }} 首</span>
      </p>

      <template v-if="s.profileLib">
        <div class="seg wide">
          <button :class="{ on: s.profileTab === 'favorites' }" @click="s.profileTab = 'favorites'; s.playlistView = null">
            喜欢的音乐 {{ s.profileLib.favorites.count }}</button>
          <button :class="{ on: s.profileTab === 'playlists' }" @click="s.profileTab = 'playlists'; s.playlistView = null">
            歌单 {{ s.profileLib.playlists.length }}</button>
          <button v-if="s.profileLib.mine" :class="{ on: s.profileTab === 'import' }"
                  @click="s.profileTab = 'import'; s.playlistView = null; loadLib()">导入</button>
        </div>

        <div class="lib-body" v-if="s.profileTab === 'favorites'">
          <p class="muted small" v-if="!s.profileLib.favorites.tracks.length">
            {{ s.profileLib.mine ? '还没有喜欢的歌。点歌时点 ♡ 就能加进来' : 'TA 还没有喜欢的歌' }}</p>
          <ul class="list">
            <li v-for="t in s.profileLib.favorites.tracks" :key="t.ref">
              <img class="thumb" v-if="t.cover" :src="t.cover" loading="lazy" alt="">
              <div class="grow"><div class="ellipsis">{{ t.title }}</div>
                <div class="muted small ellipsis">{{ t.artists }} · {{ platformName(t.platform) }}</div></div>
              <button class="icon like" :class="{ on: s.liked.has(t.ref) }" @click="toggleLike(t)">{{ s.liked.has(t.ref) ? '♥' : '♡' }}</button>
              <button class="icon" @click="addTrack(t)" title="点歌">＋</button>
            </li>
          </ul>
        </div>

        <div class="lib-body" v-else-if="s.profileTab === 'playlists'">
          <template v-if="s.playlistView">
            <div class="row">
              <button class="icon" @click="s.playlistView = null" title="返回">‹</button>
              <div class="grow ellipsis"><b>{{ s.playlistView.name }}</b>
                <span class="muted small"> · {{ platformName(s.playlistView.platform) }} · {{ s.playlistView.tracks.length }} 首</span></div>
            </div>
            <p class="muted small" v-if="s.playlistView.loading">加载中…</p>
            <p class="error small" v-if="s.playlistView.error">{{ s.playlistView.error }}</p>
            <p class="error small" v-if="s.playlistView.sync_error">同步出错：{{ s.playlistView.sync_error }}</p>
            <ul class="list">
              <li v-for="t in s.playlistView.tracks" :key="t.ref">
                <div class="grow"><div class="ellipsis">{{ t.title }} <span class="tag vip" v-if="t.vip">会员</span></div>
                  <div class="muted small ellipsis">{{ t.artists }} · {{ fmt(t.duration_ms) }}</div></div>
                <button class="icon like" :class="{ on: s.liked.has(t.ref) }" @click="toggleLike(t)">{{ s.liked.has(t.ref) ? '♥' : '♡' }}</button>
                <button class="icon" @click="addTrack(t)" title="点歌">＋</button>
              </li>
            </ul>
          </template>
          <template v-else>
            <p class="muted small" v-if="!s.profileLib.playlists.length">
              {{ s.profileLib.mine ? '还没有导入歌单。点上面的「导入」' : 'TA 还没有公开的歌单' }}</p>
            <ul class="list">
              <li v-for="p in s.profileLib.playlists" :key="p.id">
                <img class="thumb" v-if="p.cover" :src="p.cover" loading="lazy" alt="">
                <a class="grow link-row" @click="openPlaylist(p)">
                  <div class="ellipsis">{{ p.name }} <span class="tag" v-if="p.hidden">仅自己可见</span></div>
                  <div class="muted small ellipsis">{{ platformName(p.platform) }} · {{ p.track_count }} 首<span v-if="p.synced_at"> · {{ when(p.synced_at) }} 同步</span>
                    <span class="error" v-if="p.sync_error"> · 同步出错</span></div>
                </a>
                <template v-if="s.profileLib.mine">
                  <button class="icon small" @click="setHidden(p, !p.hidden)">{{ p.hidden ? '公开' : '隐藏' }}</button>
                  <button class="icon" @click="removePlaylist(p)" title="从一起听里删掉">✕</button>
                </template>
              </li>
            </ul>
          </template>
        </div>

        <div class="lib-body" v-else-if="s.profileTab === 'import'">
          <p class="muted small" v-if="!s.lib">加载中…</p>
          <template v-else>
            <div class="seg">
              <button v-for="p in s.lib.platforms" :key="p.id" :class="{ on: s.libPlatform === p.id }"
                      @click="pickLibPlatform(p.id)">{{ p.name }}</button>
            </div>
            <template v-if="libPlatformInfo()">
              <div class="account-line" v-if="libAccount() && (libAccount().logged_in || libAccount().expired || libAccount().account_id)">
                <span v-if="libAccount().logged_in">已登录 {{ libAccount().account_name || libAccount().account_id }}</span>
                <span v-else-if="libAccount().expired" class="error">登录失效了，重新扫码才能继续同步私密歌单</span>
                <span v-else class="muted">已关联 {{ libAccount().account_name || libAccount().account_id }}（只同步公开歌单）</span>
                <button class="icon small" v-if="libAccount().logged_in || libAccount().expired" @click="unbind()">解绑</button>
              </div>

              <div v-if="!s.qr && !s.importList">
                <p class="small" v-if="libPlatformInfo().login_kinds.length">扫码登录：能导入全部歌单，包括私密的和“我喜欢”，每天自动同步。</p>
                <div class="row wrap" v-if="libPlatformInfo().login_kinds.length">
                  <button v-for="k in libPlatformInfo().login_kinds" :key="k" class="primary" @click="startQr(k)">
                    {{ k === 'wx' ? '微信扫码' : k === 'qq' ? 'QQ 扫码' : '扫码登录' }}</button>
                </div>
                <p class="muted small" v-else-if="s.lib && !s.lib.enabled">扫码登录没有开启（管理员还没配置）。</p>
                <template v-if="libPlatformInfo().uid_import">
                  <p class="small">或者填{{ libPlatformInfo().uid_label }}：只能导入公开歌单。</p>
                  <form class="row" @submit.prevent="lookupUid()">
                    <input v-model="s.uid" :placeholder="libPlatformInfo().uid_label" inputmode="numeric" maxlength="20">
                    <button :disabled="s.libBusy">查找</button>
                  </form>
                </template>
              </div>

              <div class="qr" v-if="s.qr">
                <p class="muted" v-if="s.qr.loading">二维码加载中…</p>
                <template v-else>
                  <img :src="s.qr.image" alt="登录二维码">
                  <p class="small" v-if="s.qr.status === 'waiting'">{{ s.qr.hint }}</p>
                  <p class="small" v-else-if="s.qr.status === 'scanned'">扫好了，在手机上点确认</p>
                  <p class="small error" v-else-if="s.qr.status === 'expired'">二维码过期了</p>
                  <p class="small error" v-else-if="s.qr.status === 'refused'">手机上取消了</p>
                  <p class="muted small mobile-only">用的是手机？先截图，再在 App 里扫一扫 → 从相册选。</p>
                  <div class="row center-row">
                    <button v-if="s.qr.status === 'expired' || s.qr.status === 'refused'" class="primary" @click="startQr(s.qr.kind)">换一个</button>
                    <button @click="cancelQr()">取消</button>
                  </div>
                </template>
              </div>

              <div v-if="s.importList">
                <p class="small">勾选要导入的歌单（私密歌单导入后也只有你自己看得到）：</p>
                <ul class="list">
                  <li v-for="p in s.importList" :key="p.remote_id">
                    <label class="row grow">
                      <input type="checkbox" class="check" v-model="s.importPicked[p.remote_id]">
                      <span class="grow ellipsis">{{ p.name }}</span>
                      <span class="tag" v-if="p.liked">我喜欢</span>
                      <span class="tag" v-if="p.private">私密</span>
                      <span class="muted small">{{ p.track_count }} 首</span>
                    </label>
                  </li>
                </ul>
                <p class="muted small" v-if="!s.importList.length">没有可以导入的歌单</p>
                <div class="row">
                  <button class="primary" :disabled="s.libBusy" @click="doImport()">{{ s.libBusy ? '导入中…' : '导入' }}</button>
                  <button @click="s.importList = null">取消</button>
                </div>
              </div>
            </template>
            <p class="error small" v-if="s.libError">{{ s.libError }}</p>
          </template>
        </div>
      </template>
    </template>
  </div>
</div>

<div class="toast" v-if="s.toast">{{ s.toast }}</div>
`,
};

createApp(App).mount('#app');
boot();

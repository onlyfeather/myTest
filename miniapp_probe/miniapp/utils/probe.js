// 技术验证的全部逻辑。放在 app 级别（不放页面里），小程序切后台、页面隐藏时也一直在。
// 每条日志都带上手机记下它的时间，同时发给服务端；服务端用收到的时间对比，就能看出后台时有没有积压。
const { HOST } = require('../config.js');

// 同一份代码在微信和 QQ 里都能跑：QQ 小程序兼容 wx 写法，这里优先用 wx，没有再用 qq。
// eslint-disable-next-line no-undef
const api = typeof wx !== 'undefined' ? wx : qq;

const BASE = `https://${HOST}`;
const WS_URL = `wss://${HOST}/ws`;
const CLIENT = Math.random().toString(36).slice(2, 8);

const logs = [];
const listeners = new Set();
let socket = null;
let socketOpen = false;
const pending = []; // 没连上时先攒着，连上后补发
let bgm = null;
let mode = 'idle'; // idle | direct | fetch | remote
let remoteArmed = false; // 测试二：等音乐真正响起来（onPlay）才让服务端开始发指令
let nextN = 1;

function stamp(t) {
  const d = new Date(t);
  const p = (x, n = 2) => String(x).padStart(n, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}

function log(msg) {
  const t = Date.now();
  logs.unshift(`${stamp(t)}  ${msg}`);
  if (logs.length > 300) logs.pop();
  listeners.forEach((fn) => fn(logs));
  send({ type: 'log', t, msg });
}

function send(obj) {
  const data = JSON.stringify(obj);
  if (!socketOpen) {
    if (pending.length < 500) pending.push(data);
    return;
  }
  socket.send({ data, fail: () => pending.push(data) });
}

function trackUrl(n, seconds) {
  return `${BASE}/audio/${n}.wav?s=${seconds}&c=${CLIENT}`;
}

function play(n, seconds = 15) {
  nextN = n;
  bgm.title = `测试第 ${n} 首`;
  bgm.singer = '一起听 后台测试';
  bgm.epname = mode;
  // 设置 src 会自动开始播放，不用再调 play()。
  bgm.src = trackUrl(n, seconds);
  log(`设置 src：第 ${n} 首（${seconds} 秒）`);
}

function state() {
  return {
    currentTime: Math.round((bgm.currentTime || 0) * 1000) / 1000,
    paused: !!bgm.paused,
    src: (bgm.src || '').split('/').pop(),
  };
}

// 连接失败（握手超时、域名校验）时只会触发 onError，不一定有 onClose，所以两处都要安排重连。
// 同一时间只排一次，失败越多等得越久，最长 15 秒。
let retryTimer = 0;
let retryDelay = 2000;

function scheduleReconnect(reason) {
  socketOpen = false;
  if (retryTimer) return;
  log(`${retryDelay / 1000} 秒后重连 WebSocket（${reason}）`);
  retryTimer = setTimeout(() => {
    retryTimer = 0;
    connect();
  }, retryDelay);
  retryDelay = Math.min(retryDelay * 2, 15000);
}

function connect() {
  const sock = api.connectSocket({
    url: `${WS_URL}?c=${CLIENT}`,
    fail: (e) => { log(`WebSocket 连接失败：${e.errMsg}`); scheduleReconnect('连接失败'); },
  });
  socket = sock;
  log('正在连接 WebSocket…');
  sock.onOpen(() => {
    if (socket !== sock) return;
    clearTimeout(openTimer);
    socketOpen = true;
    retryDelay = 2000;
    log(`WebSocket 连上了，补发 ${pending.length} 条`);
    while (pending.length) sock.send({ data: pending.shift() });
  });
  sock.onMessage((res) => {
    let msg;
    try { msg = JSON.parse(res.data); } catch (e) { return; }
    if (msg.type === 'tick') {
      log(`收到 tick #${msg.seq}（服务端发出到收到 ${Date.now() - msg.server_ms} ms）`);
    } else if (msg.type === 'cmd') {
      runCommand(msg);
    }
  });
  sock.onClose((res) => {
    if (socket !== sock) return;
    clearTimeout(openTimer);
    log(`WebSocket 断开 code=${res && res.code}`);
    scheduleReconnect('断开');
  });
  sock.onError((e) => {
    if (socket !== sock) return;
    clearTimeout(openTimer);
    log(`WebSocket 出错：${e && e.errMsg}`);
    scheduleReconnect('出错');
  });
  // 有时既不 onOpen 也不报错，一直卡着：10 秒没连上就放弃这次，重新连。
  const openTimer = setTimeout(() => {
    if (socket !== sock || socketOpen) return;
    log('WebSocket 10 秒没连上，放弃这次重新连');
    try { sock.close({}); } catch (e) { /* 忽略 */ }
    scheduleReconnect('超时');
  }, 10000);
}

// 测试二：服务端发来的远程控制，执行后马上回报播放器状态。
function runCommand(msg) {
  log(`收到指令 #${msg.id} ${msg.action}`);
  try {
    if (msg.action === 'pause') bgm.pause();
    else if (msg.action === 'play') bgm.play();
    else if (msg.action === 'seek') bgm.seek(msg.position);
    else if (msg.action === 'src') play(msg.n, msg.s);
  } catch (e) {
    log(`指令 #${msg.id} 抛出异常：${e && e.message}`);
  }
  // 播放器状态更新有延迟，过一会儿再读。
  setTimeout(() => send({ type: 'ack', t: Date.now(), id: msg.id, action: msg.action, ...state() }), 800);
}

function onEnded() {
  log(`第 ${nextN} 首放完了（onEnded）`);
  const n = nextN + 1;
  if (mode === 'direct') {
    // 测试一 A：一首放完马上换下一首，中间不联网。
    play(n);
  } else if (mode === 'fetch') {
    // 测试一 B：先请求服务端拿“下一首”，再换。看后台能不能发起网络请求。
    const started = Date.now();
    api.request({
      url: `${BASE}/next?n=${n}`,
      success: (res) => {
        log(`/next 请求成功，用了 ${Date.now() - started} ms`);
        play(res.data && res.data.n ? res.data.n : n);
      },
      fail: (e) => {
        log(`/next 请求失败：${e.errMsg}（用了 ${Date.now() - started} ms）`);
      },
    });
  } else {
    log('当前模式不自动换歌');
  }
}

function start() {
  bgm = api.getBackgroundAudioManager();
  bgm.onPlay(() => {
    log(`onPlay ${state().src}`);
    if (remoteArmed) {
      // 音乐已经在放，小程序切后台不会被挂起，这时再开始计时。
      remoteArmed = false;
      send({ type: 'remote_test' });
      log('音乐已经响了：现在锁屏，服务端 25 秒后开始发指令');
    }
  });
  bgm.onPause(() => log(`onPause，位置 ${state().currentTime}s`));
  bgm.onStop(() => log('onStop（系统或用户关闭了播放器）'));
  bgm.onEnded(onEnded);
  bgm.onError((e) => log(`onError ${e && (e.errCode || e.errMsg)}`));
  bgm.onWaiting(() => log('onWaiting（缓冲中）'));
  bgm.onSeeked(() => log(`onSeeked，位置 ${state().currentTime}s`));
  if (bgm.onNext) bgm.onNext(() => log('系统面板点了下一曲'));
  // 每 10 秒报一次心跳：后台时 JS 如果被冻住，服务端就会看到这里断档。
  setInterval(() => {
    const s = state();
    log(`心跳 ${s.paused ? '暂停' : '播放中'} ${s.currentTime}s ${s.src} WS:${socketOpen ? '已连' : '未连'}`);
    if (!socketOpen && !retryTimer) scheduleReconnect('心跳发现没连上');
  }, 10000);
  connect();
  log(`启动，客户端编号 ${CLIENT}`);
}

module.exports = {
  start,
  log,
  logs: () => logs,
  subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  client: () => CLIENT,
  runDirect() { mode = 'direct'; log('== 测试一 A：放完直接换下一首 =='); play(1); },
  runFetch() { mode = 'fetch'; log('== 测试一 B：放完先联网再换 =='); play(1); },
  runRemote() {
    // 指令全靠 WebSocket 下发，没连上时开始就白测了。
    if (!socketOpen) {
      log('测试二没开始：WebSocket 还没连上（正在重连），等日志里出现「WebSocket 连上了」再点，不用连点');
      return;
    }
    mode = 'remote';
    remoteArmed = true;
    log('== 测试二：远程控制。等听到声音、日志出现「音乐已经响了」再锁屏 ==');
    play(1, 180);
  },
  stop() { mode = 'idle'; remoteArmed = false; bgm.stop(); log('手动停止'); },
  // 打开调试后不校验合法域名（测试域名没备案，不开就连不上），小程序会自动重启。
  enableDebug() {
    api.setEnableDebug({
      enableDebug: true,
      fail: (e) => log(`打开调试失败：${e && e.errMsg}`),
    });
  },
};

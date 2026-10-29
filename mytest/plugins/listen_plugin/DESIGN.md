# 一起听 设计文档

状态：M1 已完成（见第 20 节），M2 待开始。最后更新 2026-10-29。

## 1. 目标与范围

白名单群的群友在独立域名的网页上同步听同一首歌，可以自由点歌、切歌、暂停、拖进度、聊天。
凛音知道本群正在听什么，被问到时简要评价，能发音乐卡片，能按要求推荐歌并加进队列。

不做：公开房间、访客身份、凛音主动聊起正在听的歌、凛音在群里说出某人的歌单内容。

## 2. 需求

### 功能需求

| 编号 | 需求 |
|---|---|
| F1 | 只有白名单里的群能开一起听；只有这些群的成员能进入，每个群一个房间 |
| F2 | 身份：在群里发 `/一起听`，凛音私聊一条一次性登录链接（方案 A）；私聊发不出时用验证码登录（方案 B） |
| F3 | 同步播放：播放、暂停、拖进度、切歌所有人同步；新进来的人从当前进度开始 |
| F4 | 播放控制默认所有人可用，房主可以关掉（关掉后只有房主和 superuser 能控制） |
| F5 | 队列：点歌、删歌、调顺序；搜索网易云、QQ 音乐、酷狗 |
| F6 | 聊天、在线成员、资料卡（头像、昵称、群名片、头衔、签名、等级、“喜欢的音乐”和歌单） |
| F7 | 每人一个“喜欢的音乐”；可以填用户 ID 导入公开歌单，或扫码登录导入全部歌单（自己勾选导入哪些） |
| F8 | 扫码得到的平台凭证加密保存，每天自动刷新并同步已选歌单 |
| F9 | 歌词滚动显示 |
| F10 | 凛音：知道本群正在放的歌和队列；发音乐卡片；按要求推荐歌（有房间就加进队列，没有就发卡片） |
| F11 | 手机和电脑都能用 |

### 非功能需求

| 编号 | 需求 |
|---|---|
| N1 | 听众之间进度差 ≤ 300ms；断线重连后自动回到当前进度 |
| N2 | 每个房间默认上限 10 人（预期 7 人），可在 WebUI 改 |
| N3 | 带宽 5Mbps、流量 500GB/月：默认 320kbps，在线超过 10 人降到 128kbps |
| N4 | 独立端口、独立域名，不复用 WebUI 的路由、会话和 cookie |
| N5 | 一起听出错（端口占用、音源挂掉、密钥缺失）不影响 bot 其他功能 |
| N6 | 平台凭证不出现在日志、接口返回和浏览器里 |

## 3. 架构

```
浏览器 (Vue 3 页面 + <audio>)
   │ HTTPS: 页面、音频、搜索、歌单
   │ WSS:   播放状态、聊天、对时
   ▼
反代 (Caddy / Nginx)  listen.<域名>
   ▼
:8090  一起听 FastAPI app ──────────────┐   同一个 NoneBot 进程
:8080  NoneBot (OneBot WS、/webui、/tools) │
          │                               │
          ├─ BotBridge ◄──────────────────┘  发私聊/群消息、查群成员、查资料
          ├─ 音源
          │    ├─ QQ 音乐：qqmusic-api-python（进程内）
          │    ├─ 网易云：api-enhanced（Node 容器，HTTP）
          │    └─ 酷狗：KuGouMusicApi（Node 容器，HTTP）
          └─ SQLite（localstore 数据目录 listen.db）

MaiBot ── rinne_tools ── POST /tools/music_* ──► NoneBot（复用现有工具接口）
```

### 运行方式

NoneBot 启动时，在同一个事件循环里再起一个 `uvicorn.Server`，监听 `LISTEN_PORT`。
`app.py` 的 `create_app(bridge, store, sources)` 不 import nonebot，所有和 bot 相关的操作经过 `BotBridge` 协议，
以后拆成独立服务只需要换启动方式。

第二个 uvicorn 关闭信号接管（`install_signal_handlers` 置空），由 NoneBot 的 shutdown 钩子设置 `should_exit` 停止。
端口被占用或缺少必需的环境变量时只打警告、不启动。

### 目录结构

```
listen_plugin/
  __init__.py        插件入口：群指令、帮助、设置项、启动第二个 uvicorn
  config.py          环境变量与 WebUI 设置项
  bridge.py          BotBridge：NoneBot 侧实现
  app.py             create_app()：路由、WebSocket、静态文件
  auth.py            登录链接签名、验证码、会话、成员资格检查
  room.py            房间状态机（纯逻辑）
  sync.py            进度计算、对时（纯逻辑）
  store.py           SQLite 读写
  crypto.py          凭证加解密
  jobs.py            每日刷新凭证、同步歌单
  recommend.py       凛音推荐：LLM 选歌 + 搜索核对
  card.py            音乐卡片（签名服务 → json 消息段；失败退回图文）
  sources/
    base.py          MusicSource 协议、Track 等数据类
    qqmusic.py
    netease.py
    kugou.py
    resolver.py      跨平台找同一首歌、选音源
  static/            index.html、app.js、各组件、vendor/vue.esm-browser.prod.js
  tests_*.py
```

## 4. 技术选型

| 方面 | 选择 | 理由 |
|---|---|---|
| 服务端 | FastAPI（已有依赖） | 和 NoneBot 同一套，WebSocket 自带 |
| 实时通信 | WebSocket | 双向、延迟低；SSE 要拆两条通道 |
| 前端 | **Vue 3，本地放一份 ESM 构建文件，不用构建工具** | 功能比最初设想多（资料卡、歌单、扫码导入、歌词、搜索），原生 JS 状态管理会变乱；不引入 Node 构建；文件放本地，不依赖 CDN（国内 CDN 不稳定） |
| 播放 | `<audio>` + Media Session API | 支持 `playbackRate` 做纠偏，锁屏可控 |
| 存储 | **SQLite**（标准库 `sqlite3`，和 dice、pixiv 插件一致） | 有了用户、歌单、凭证，JSON 快照不够用 |
| 加密 | `cryptography` 的 AES-256-GCM | 标准、带完整性校验；新增依赖 |
| QQ 音乐 | `qqmusic-api-python` | Python 异步库，进程内调用；新增依赖 |
| 网易云 / 酷狗 | api-enhanced / KuGouMusicApi，各一个 Node 容器 | 目前维护中的非官方实现 |

新增 Python 依赖：`cryptography`、`qqmusic-api-python`。

## 5. 身份与权限

### 方案 A：一次性登录链接

1. 群友在白名单群发 `/一起听`。没有房间就开一个，发起人当房主。
2. bot 私聊发送 `https://listen.<域名>/login?t=<token>`。token = HMAC-SHA256(`LISTEN_SECRET`)，内容是 QQ 号、群号、过期时间（10 分钟）、随机 jti。
3. 打开链接：校验签名和过期时间，jti 写入已用表（只能用一次），检查成员资格，签发会话 cookie，`history.replaceState` 去掉 URL 里的 token。
4. 私聊发不出：在群里回复“私聊发不出去，打开 https://listen.<域名> 用验证码登录”。

### 方案 B：验证码

1. 打开登录页，页面通过 WebSocket 拿到一个 6 位码（5 分钟有效，同一个浏览器同时只有一个）。
2. 用户在私聊或白名单群里发 `/一起听 登录 123456`。bot 由发送者确定 QQ 号，检查成员资格，通知那个页面登录。
3. 登录后页面显示“已登录为 xx（头像）”，有“不是我，重新登录”按钮。
4. 输错码不提示是否存在，同一 QQ 每分钟最多尝试 5 次。

### 会话

- cookie：`HttpOnly; Secure; SameSite=Lax`，30 天，只在一起听域名下。数据库只存会话 ID 的 SHA-256。
- 每次建立 WebSocket 连接时检查成员资格（`get_group_member_info`，结果缓存 10 分钟）。退群、被踢、群移出白名单：立即断开，会话作废。
- 一个人在多个白名单群：登录后选群。

### 权限

| 操作 | 群成员 | 房主 | superuser |
|---|---|---|---|
| 听、聊天、点歌、管理自己的歌单 | ✓ | ✓ | ✓ |
| 播放、暂停、拖进度、切歌、删别人点的歌 | 默认 ✓，房主可关 | ✓ | ✓ |
| 开关“所有人可控制” | | ✓ | ✓ |
| 关闭房间（`/一起听 关闭`） | | ✓ | ✓ |
| 登录 bot 的平台会员账号 | | | ✓ |

防乱操作：聊天区显示每次控制操作（“xx 切到了下一首”）；每人每 10 秒最多 3 次控制操作。

### 白名单

WebUI 新增「一起听」分区（`settings_store`），填群号列表。为空时不开放。

## 6. 房间与同步

### 房间状态（服务端为准）

```
RoomState {
  group_id, owner_qq, allow_control
  track: TrackRef | None        # platform + id
  paused: bool
  anchor_server_ms: int         # 最近一次状态变化的服务端时间
  anchor_position_ms: int       # 那时的播放位置
  version: int                  # 每次变化 +1
}
```

当前进度 = `anchor_position_ms + (now_ms - anchor_server_ms)`（暂停时不加）。

### 同步算法

1. 对时：连接后发 5 次 `ping`，取往返最短的一次估算时钟偏差；之后每 30 秒一次。
2. 每秒比较本地进度和应有进度：
   - 偏差 < 300ms：不处理
   - 300ms–2s：`playbackRate` 临时设为 0.95 / 1.05 追平
   - > 2s：直接跳到应有位置
3. 控制指令带 `version`，过期的丢弃并把最新状态推回给发送方。
4. 一首放完：由服务端计时器切下一首，不依赖客户端上报，避免多人同时上报导致连跳。
5. 浏览器自动播放限制：进房后点一下“开始收听”。

### 生命周期

- 最后一人离开 30 分钟后关闭房间，队列保留，下次开房恢复。
- 房间状态每次变化写入 SQLite，bot 重启后恢复（恢复为暂停状态）。

## 7. 消息协议（WebSocket，JSON，Pydantic 校验）

客户端 → 服务端

| type | 字段 |
|---|---|
| `ping` | `client_ms` |
| `control` | `action: play\|pause\|seek\|next\|prev`, `position_ms?`, `version` |
| `queue` | `op: add\|remove\|move`, `track?`, `item_id?`, `to?` |
| `chat` | `text`（≤ 200 字） |

服务端 → 客户端

| type | 字段 |
|---|---|
| `pong` | `client_ms`, `server_ms` |
| `state` | RoomState + 当前歌曲信息 |
| `queue` | 队列全量 |
| `members` | 在线成员 |
| `chat` | `from`, `text`, `ts`；控制操作也作为系统消息发 |
| `error` | `code`, `message` |

每个连接每秒最多 5 条消息，超过的丢弃。

HTTP 接口：`/login`、`/api/me`、`/api/search`、`/api/tracks/{ref}/lyrics`、`/api/favorites`、`/api/playlists`、
`/api/import/{platform}/qr`（发起、轮询）、`/api/import/{platform}/uid`、`/api/credentials`（查看、解绑）、`/audio/{ref}`、`/avatar/{qq}`。

## 8. 音源

### 接口

```python
class MusicSource(Protocol):
    platform: str                                    # "qq" | "netease" | "kugou"
    async def search(self, keyword: str, limit: int) -> list[Track]: ...
    async def track(self, track_id: str) -> Track: ...
    async def stream(self, track_id: str, quality: Quality, cred: Credential | None) -> Stream: ...
    async def lyrics(self, track_id: str) -> Lyrics | None: ...
    async def qr_start(self) -> QrSession: ...
    async def qr_poll(self, session: QrSession) -> Credential | None: ...
    async def refresh(self, cred: Credential) -> Credential: ...
    async def playlists(self, *, uid: str | None = None, cred: Credential | None = None) -> list[Playlist]: ...
    async def playlist_tracks(self, playlist_id: str, cred: Credential | None) -> list[Track]: ...
```

`Track` 用 `platform:id` 标识，带歌名、歌手、专辑、封面、时长、是否需要会员。

### 选源和降级

- 搜索三家并发，各自超时 5 秒，结果按平台分组显示，挂掉的平台不影响其他平台。
- 一首歌在原平台拿不到播放地址（下架、会员、地区限制）时，到其他平台按“歌名 + 歌手 + 时长 ±3 秒”找同一首。
- 每个平台连续失败 5 次后熔断 5 分钟。

### bot 的会员账号

superuser 在一起听页面的管理区扫码登录 bot 自己的平台账号，凭证按第 10 节加密保存，用于播放会员歌曲。
这些接口都不是官方的，建议用小号。

### 音频分发

- 每首歌在开始播放前才取地址，避免过期。
- 非会员歌：浏览器先直连（`referrerpolicy="no-referrer"`），失败自动改走 `/audio/{ref}`。
- 会员歌：一律走 `/audio/{ref}`，播放地址不发给浏览器。
- `/audio/{ref}`：服务端下载一次，写入磁盘缓存（LRU，默认 2GB），支持 Range。同一首歌多人同时听只下载一次。
- 默认 320kbps；在线超过 10 人改用 128kbps。

## 9. 歌单

- 每人默认一个“喜欢的音乐”，页面上点 ♥ 加入。
- 填用户 ID 导入：网易云用户 ID、QQ 音乐的 QQ 号、酷狗用户 ID，只能读公开歌单。
- 扫码登录导入：列出全部歌单（包括私密歌单和平台上的“我喜欢”），用户勾选。手机上提示“截图后在 App 里从相册识别”。
- 已选的歌单每天同步一次；导入的歌单在一起听里只读，可以把里面的歌点进队列或加到“喜欢的音乐”。
- 资料卡像音乐 App 的个人主页：显示对方的“喜欢的音乐”和导入的歌单，同群成员点开能浏览，可以把里面的歌点进队列或加到自己的“喜欢的音乐”。
  本人可以把单个歌单设为“仅自己可见”（默认可见）；导入时平台上标为私密的歌单，默认设为仅自己可见。
- 别人看不到的只有凭证、绑定的平台账号 ID。
- 凛音推荐时只把**提问人自己的**歌单当参考；凛音不主动在群里说某人的歌单里有什么。

## 10. 凭证加密

- 算法：AES-256-GCM。密钥来自环境变量 `LISTEN_CREDENTIAL_KEY`（32 字节，base64），和 `LISTEN_SECRET` 分开。
- 每条凭证随机 12 字节 nonce；附加数据（AAD）为 `qq:platform:key_version`，防止把别人的密文挪到自己名下。
- 表里存 `ciphertext, nonce, key_version, status, expires_at, updated_at`；明文只在刷新、同步、取播放地址时在内存里出现。
- 日志、异常信息、接口返回里不出现凭证；对 httpx 的请求日志做脱敏。
- 密钥轮换：新密钥写入 `LISTEN_CREDENTIAL_KEY`，旧密钥放 `LISTEN_CREDENTIAL_KEY_OLD`，后台任务逐条重新加密后删除旧密钥。
- 密钥丢失：所有凭证作废，用户重新扫码；歌单数据不受影响。
- 每日任务：刷新凭证 → 同步已选歌单。刷新失败标记为“已失效”，页面提示重新扫码，不发 QQ 消息。
- 用户可以在页面上随时解绑，立即删除凭证；用户不再属于任何白名单群时，凭证立即删除。

## 11. 凛音

### 上下文注入（只在被问到时提起）

`rinne_tools` 在 `maisaka.replyer.before_request` 钩子里调用 `/tools/listen_context`（超时 1 秒，缓存 10 秒），
只在白名单群且有房间时注入，约 150 字：

```
【一起听】本群正在一起听：歌名 - 歌手（1:23/4:05），接下来：A、B、C。
只在有人问起时再提，不要主动说。
```

### 工具

| 工具 | 什么时候用 | NoneBot 做什么 | 范围 |
|---|---|---|---|
| `rinne_music_share` | 有人让她发某首歌 | 搜索 → 发音乐卡片 | 所有群 |
| `rinne_music_recommend` | 有人让她推荐歌 | 第 11.3 节流程；有房间就加进队列并在群里说一句，没有就发卡片 | 所有群；加队列只在白名单群 |
| `rinne_listen_status` | 有人问刚才放了什么、队列里还有什么 | 返回本群播放历史和完整队列 | 白名单群 |

评价歌曲由凛音用自己的知识回答，不需要单独的工具。

### 推荐流程

1. LLM 输入，按优先级：
   1. 提问人的要求（硬性条件：曲风、心情、歌手、语言、年代、“像某首歌那样”）
   2. 要求没限定的部分才参考：本群最近 20 条聊天（NoneBot 按群维护一个环形缓冲区，只缓存白名单群，不落盘）、本群最近播放、提问人自己的歌单摘要
   3. 冲突时按要求来；排除本群最近 50 首放过的歌
2. LLM 返回 JSON：`[{title, artist}]`，数量为请求数的 2 倍。格式不对静默重试，最多 3 次（和画图翻译 tag 的做法一致）。
3. 逐个搜索，歌名和歌手都对上才算数（防止编造歌名），取够数量为止；默认 1 首，最多 5 首。
4. 凛音回复时不提“因为你歌单里有……”。

### 音乐卡片

卡片是 QQ 的 Ark 消息（`com.tencent.music.lua`），必须带腾讯服务端签发的 `config.token`，只能由签名服务生成。

NapCat 最新源码（`packages/napcat-onebot/api/msg.ts`）里，`musicSignUrl` 留空时首选 `http://106.55.0.102:10087/`，
失败再用 `https://ss.xingzhige.com/music_card/card`。2026-10-29 按 NapCat 的请求格式实测：

| 请求 | 106.55.0.102:10087 | ss.xingzhige.com |
|---|---|---|
| `{type, id}`（只给歌曲 ID） | 400 `缺少 title` | 空响应 / 报错 |
| 自定义（url、audio、title、image、singer） | 200，返回带 token 的 Ark JSON，`type` 填 qq / 163 / kugou 时卡片角标分别是 QQ音乐 / 网易云音乐 / 酷狗音乐 | 空响应 |

做法：发 `music` 消息段时**一律用自定义格式**（我们本来就有歌名、歌手、封面、跳转地址和音频地址），`type` 按平台填，由 NapCat 去签名。
不用只给 ID 的格式。签名失败（NapCat 返回 ActionFailed）时退回发“封面图 + 歌名 - 歌手 + 链接”。

卡片里的 `audio` 是 QQ 客户端直接播放的地址。卡片只需要在发出后约 30 分钟内能播放（基本不会翻聊天记录回去听），
所以直接填平台的直链，不经过一起听的服务器，也不开放不需要登录的转发接口：

| 平台 | `audio` 填什么 | 有效期（2026-10-29 实测） |
|---|---|---|
| 网易云 | 官方外链 `https://music.163.com/song/media/outer/url?id={id}.mp3` | 外链本身长期有效，每次请求现场跳转到新直链（直链约 25 分钟过期） |
| QQ 音乐 | 发卡片时用 bot 的账号凭证取直链 | 接口返回 `expiration=7200`，约 2 小时；不带凭证取不到（`result=104003`） |
| 酷狗 | 发卡片时取直链（免费歌不用登录：`m.kugou.com/app/i/getSongInfo.php?cmd=playInfo`；付费歌用会员凭证） | 直链 `sharefs.kugou.com/{时间戳}/{签名}/...`，时间戳被签名，不能改；实测至少 40 分钟 |

会员歌也放完整直链（已确认）：bot 在三个平台各配一个会员账号，发卡片时用对应平台的会员凭证取地址。

取不到直链（下架、会员凭证失效、平台接口挂了）时，卡片照发，`audio` 留空或填试听片段，点卡片跳到平台歌曲页。

注意：这个签名服务是第三方公益服务，没有可用性保证。

## 12. 安全

- 所有外部输入用 Pydantic 校验并限制长度；聊天、歌名、昵称前端只用文本插值渲染（Vue 默认转义，禁止 `v-html`）。
- WebSocket 握手检查 Origin 必须等于 `LISTEN_PUBLIC_URL`，并检查会话。
- 只有来自 `LISTEN_TRUSTED_PROXIES` 的请求才采信 `X-Forwarded-For`。
- 头像经 `/avatar/{qq}` 转发，只允许同群成员的 QQ 号，带缓存。
- `/audio/{ref}` 的 ref 用白名单正则校验，缓存文件名由服务端生成。
- 安全响应头：CSP（`default-src 'self'`，媒体源放开三家音频域名）、`X-Content-Type-Options`、`Referrer-Policy: no-referrer`。
- SQL 全部参数化。

## 13. 配置

环境变量（敏感项和部署项）：

```
LISTEN_PORT=8090
LISTEN_HOST=127.0.0.1
LISTEN_PUBLIC_URL=https://listen.example.com
LISTEN_SECRET=至少 32 位随机串            # 缺少则不启动
LISTEN_CREDENTIAL_KEY=32 字节 base64      # 缺少则不启动扫码导入，其他功能正常
LISTEN_TRUSTED_PROXIES=127.0.0.1
NETEASE_API_URL=http://netease-api:3000
KUGOU_API_URL=http://kugou-api:3000
```

WebUI「一起听」分区：白名单群、房间人数上限、默认音质、降级阈值、缓存大小。

## 14. 部署

- docker-compose 增加 `netease-api`、`kugou-api` 两个服务，只在内部网络暴露。
- NoneBot 容器映射 `127.0.0.1:8090:8090`。
- 反代：转发 WebSocket Upgrade，`proxy_read_timeout` ≥ 3600s，`/audio/` 关闭响应缓冲。随代码附 Caddy 和 Nginx 示例。

## 15. 测试

| 层 | 内容 |
|---|---|
| 单元 | `room.py` 状态机、`sync.py` 进度和纠偏判断、token 签名与一次性校验、验证码、AES-GCM 加解密与 AAD 篡改、推荐结果核对、跨平台匹配 |
| 集成 | FastAPI `TestClient` 双 WebSocket 客户端：状态广播、版本冲突、权限开关、限流、断线重连、成员资格失效踢出；音源用假实现 |
| 契约 | 三个音源对真实接口的冒烟测试，默认跳过，手动开启 |
| 手动 | 电脑 + 手机同时听，测进度差、锁屏控制、扫码导入、音乐卡片 |
| 静态检查 | ruff、pyright（沿用 `pyproject.toml`） |

## 16. 里程碑

| 阶段 | 内容 | 完成标准 |
|---|---|---|
| M1 | 独立服务、白名单、登录 A + B、房间同步、QQ 音乐搜索和播放、队列、基础页面（手机 + 电脑） | 两个设备能同步听 QQ 音乐的歌 |
| M2 | 网易云、酷狗、跨平台降级、音频转发和缓存、聊天、成员、资料卡、歌词、控制权限开关 | 三家都能搜能放 |
| M3 | 喜欢的音乐、用户 ID 导入、扫码导入、凭证加密保存、每日刷新同步、bot 会员账号 | 扫码导入后第二天能自动同步 |
| M4 | 凛音：上下文注入、`rinne_music_share`、`rinne_music_recommend`、`rinne_listen_status`、音乐卡片 | 群里让凛音推荐歌能进队列 |

## 17. 风险

| 风险 | 影响 | 应对 |
|---|---|---|
| 非官方接口失效 | 某个平台不能用 | 接口隔离、熔断、跨平台降级；依赖版本锁定，失效时升级 |
| 平台风控封号 | bot 会员账号或用户账号受影响 | bot 用小号；用户凭证只做每天一次的刷新和同步，不做高频请求 |
| 音乐卡片签名服务不稳定 | 卡片发不出 | 自动退回图文消息 |
| 第二个 uvicorn 和 NoneBot 的生命周期冲突 | 关不掉或连带崩溃 | M1 首先验证；异常隔离 |
| 5Mbps 带宽 | 人多时卡顿 | 磁盘缓存、自动降音质、人数上限 |

## 18. 已确认（2026-10-29）

1. 会员歌的卡片可以完整播放：卡片里放用 bot 会员账号取到的直链。
2. bot 在网易云、QQ 音乐、酷狗各配一个会员账号。
3. 资料卡显示对方的歌单，和音乐 App 的个人主页一样（见第 9 节）。
4. MaiBot 和 NoneBot 用同一个 QQ 号：私聊链接、卡片都由 NoneBot 发出。

## 19. 待验证

1. ~~酷狗免费歌直链的有效期~~：2026-10-29 实测取到后 0/10/20/30/40 分钟都能下载（206），至少 40 分钟，够卡片用。
2. 酷狗、网易云会员歌在 bot 会员账号下能否取到完整直链（部署 KuGouMusicApi、api-enhanced 并扫码登录后测）。
3. 音乐卡片实际发进 QQ 群的显示和播放效果（需要在 bot 上测）。

## 20. M1 实现记录（2026-10-29）

已完成：独立端口服务、白名单、登录（一次性链接 + 验证码）、房间同步、QQ 音乐搜索和播放、队列、手机/电脑页面。

和设计的差异：

- 聊天、在线成员原定 M2，顺手做进了 M1（逻辑很少）；歌词、资料卡、网易云/酷狗仍在 M2。
- 前端 CSP 需要 `'unsafe-eval'`：不用构建工具时 Vue 在浏览器里编译模板要用 `new Function`。模板只来自 `static/`，用户内容都是文本插值。
- 锁屏/耳机/系统媒体键的暂停只停自己，不替全房间暂停（拔耳机、来电时系统也会发暂停）；切歌照常同步给所有人。
- 会员歌在配置会员账号（M3）之前点不了：放不出来的歌进队列会卡住所有人。正在放的歌拿不到地址时，服务端自动跳过并在聊天区说明。
- 群移出白名单后，最迟一分钟内关闭房间、作废登录。

验证结果：

- 单元 + 集成测试 `tests/test_listen.py` 23 个通过；全量 162 通过、10 跳过。
- 真实 NoneBot 进程：一起听监听自己的端口，NoneBot 端口上访问不到；Ctrl+C 后 NoneBot 和一起听都正常退出；端口被占用时只打日志，bot 照常运行。
- 浏览器 + 第二个 WebSocket 客户端：对方播放、暂停、拖进度都能同步，本机测得进度差约 200ms；放完自动切下一首。

### 部署

1. `.env` 或 docker-compose 环境变量：

   ```
   LISTEN_SECRET=至少 32 位随机字符串
   LISTEN_PUBLIC_URL=https://listen.你的域名
   LISTEN_HOST=0.0.0.0          # Docker 里需要；裸机部署保持 127.0.0.1
   LISTEN_PORT=8090
   LISTEN_TRUSTED_PROXIES=反代访问容器时的来源 IP（Docker 网桥一般是 172.17.0.1）
   ```

2. docker-compose 端口映射：`127.0.0.1:8090:8090`（只给本机反代访问）。
3. WebUI「一起听」里填白名单群号。
4. 反代（Caddy 示例，自动 HTTPS，WebSocket 自动转发）：

   ```
   listen.你的域名 {
       reverse_proxy 127.0.0.1:8090
   }
   ```

   Nginx 需要手动转发 WebSocket：

   ```
   location / {
       proxy_pass http://127.0.0.1:8090;
       proxy_http_version 1.1;
       proxy_set_header Upgrade $http_upgrade;
       proxy_set_header Connection "upgrade";
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_read_timeout 3600s;
   }
   ```

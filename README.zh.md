<sub><a href="README.md">🌐 English</a> · <b>中文</b></sub>

# slack-log

把一个 Slack workspace 变成**带永久 ref-id 锚点的静态 IRC log 风格 HTML viewer**，
同时输出一份**给 AI / shell `grep` 直接读的 JSONL 数据层**。

### 我为什么做这个

经常要翻老的 Slack thread —— 给新人补背景、写周报、找三个月前某个决策的原话。Slack
自家搜索免费版限制 1 万条，付费版也没法把一个 thread `grep` 出来或者喂给 AI。

现有工具都半截：[slackdump](https://github.com/rusq/slackdump) 把最难的部分（认证、限流、
增量 resume）做完了，但输出是 SQLite + 按日期分的 JSON，不是「一个 thread 一份文件」。
[slack-export-viewer](https://github.com/hfaran/slack-export-viewer) 是个常驻 Flask
server。两者都没有「每条消息一个稳定锚点」，没法贴一个 URL 到文档里指望它一年后还指向同一条消息。

所以 slack-log **站在 slackdump 上面**，只做 slackdump 不做的事：

1. 把 SQLite 切成一个 thread 一份 JSONL，文件名直接用 `thread_ts`（Slack 的稳定唯一 id）。
2. 渲染静态 HTML，每条消息都有 `<a id="msg-{ts}">` 锚点 —— 一个 URL 贴到任何文档里，
   一年后还是指向同一条消息。
3. 附件按 mime / 大小阈值差异化下载（图片下，大 zip 只存 metadata）。
4. 把所有 uid / cid 解析成显示名，渲染 mrkdwn / 链接 unfurl 卡片 / reactions popup /
   lightbox，效果接近 Slack 原生。

### 你可能想要它的几个理由

- **一个 thread 一份纯 JSONL 文件**。`data/channels/<cid>/threads/<thread_ts>.jsonl`，
  Slack API 字段全保留（blocks / reactions / files / edited / attachments），每行一条
  完整消息。`grep` / `jq` / AI prompt 直接读。
- **永久 ref id**。每条消息 `<a id="msg-{ts}">`，URL 形如
  `…/threads/1779079280.797169.html#msg-1779154899.648009`，可以放心贴到任何文档当引用。
- **没后端**。`python3 render.py` 出一份静态 HTML，`file://` 或任意静态服务都能开。
  lightbox / 排序切换 / reactions popup 全是纯 vanilla JS（~60 行，零依赖）。
- **站在 slackdump 肩膀上**。认证 / 限流 / 增量 resume / thread reply 晚到检测 — 都
  交给 slackdump，slack-log 只 subprocess 调它。
- **双格式并存**。JSONL 给 AI / shell，SQLite（slackdump 原版）保留给 SQL / 未来后端。
- **选择性渲染**。`make render-channels` 跳过 DM/MPIM，分享频道存档不会带出私聊。

> 个人项目，MIT 协议。只在我自己的一个 Slack workspace + macOS 上测过。Public / private
> channel / DM / MPIM 都能跑，Bot 账户没测。

## 快速上手

```sh
# 1. 装依赖
brew install slackdump
pip install pyyaml jinja2 emoji

# 2. 给 slackdump 配 Slack 凭据（浏览器 cookie 拿 xoxc + xoxd）
#    最简方式见 https://github.com/rusq/slackdump/wiki/EZ-Login-3000
slackdump workspace new

# 3. 拉到 raw/slackdump.sqlite
make fetch

# 4. 全套构建（SQLite → JSONL，下载附件，渲染 HTML）
make update

# 5. 起静态服务
cd html && python3 -m http.server 8765
open http://localhost:8765/
```

## 常用命令

```sh
make update              # 完整增量：fetch → split → attach → render
make fetch               # 只跑 slackdump archive --resume（便宜，加性）
make reconcile           # 重拉最近 90 天兜底编辑/删除（每周跑一次）
make rebuild-html        # 改了模板/CSS 后用这个 — 保留 data/，最快路径
make render-channels     # 只渲染真频道（跳过 DM 和 MPIM）
make render-dms          # 只渲染 DM
make help                # 看全部 target
```

## 架构

```
┌──────────────────────────────────────────────────┐
│  下游消费者：周报 / AI runbook / 历史检索         │
└──────────────────────────────────────────────────┘
                       ↑
┌──────────────────────────────────────────────────┐
│  视图 + skill 层                                  │
│  HTML（jinja2 + ref id + 排序切换 + lightbox）    │
│  Claude skill（读 JSONL，不调 Slack API）         │
└──────────────────────────────────────────────────┘
                       ↑
┌──────────────────────────────────────────────────┐
│  数据层(双格式并存)                                │
│  thread JSONL  ← AI / grep                       │
│  channel index.jsonl + users.json + channels.json│
│  slackdump.sqlite ← SQL / 未来后端                │
│  attachments/(按阈值差异化下载)                    │
└──────────────────────────────────────────────────┘
                       ↑
┌──────────────────────────────────────────────────┐
│  采集层(slackdump subprocess)                     │
│  archive + Lookback resume + 限流 + 认证          │
└──────────────────────────────────────────────────┘
```

slack-log 自己 **~900 行 Python + Jinja2**，采集层完全外包给 slackdump（AGPLv3 binary，
subprocess 调用 — 不污染 slack-log 的 MIT 协议）。

## 文件清单

| 路径 | 说明 |
|---|---|
| `slack_log/splitter.py` | `slackdump.sqlite` → 每 thread 一份 JSONL + `users.json` + `channels.json` |
| `slack_log/attach.py` | 扫 JSONL 的 files 字段，按 mime/size 策略差异化下载，每个文件都生成 `.meta.json` |
| `slack_log/render.py` | JSONL → 静态 HTML，解析 mention / mrkdwn，构建 reaction popup 和 unfurl 卡片 |
| `slack_log/templates/` | `_base.html`（CSS + lightbox JS）+ `global_index` / `channel_index` / `thread.html` |
| `tests/` | pytest 测试套件（splitter / attach / render 错误恢复测试） |
| `pyproject.toml` | Package 元信息、依赖、console scripts、pytest 配置 |
| `Makefile` | 构建 target（`update` / `fetch` / `reconcile` / `rebuild-html` / `render-channels` / `test`...） |

## 设计原则

- **附件跨 HTML rebuild 保留**。`data/` 是宝贵的（重下载很慢），`html/` 廉价。
  `make rebuild-html` 只动 `html/`。只有 `make clean-all` 才会删 `data/`。
- **稳定文件名**。Thread 文件用 `thread_ts`（Slack 唯一 id）命名 — 不是日期不是 preview，
  URL 永不变。
- **`.meta.json` 永远生成**。大 zip 和视频只存 metadata，原 Slack URL 保留，未来想下能下。
- **HTML 渲染懂 `<a>` 嵌套规则**。Slack mrkdwn URL 出现在 channel index 的 preview 段时
  会降级成 `<span>`，避开 HTML 规范禁止 `<a>` 嵌套 `<a>` 导致的隐式闭合问题。
- **编辑/删除靠重拉兜底，不靠 event**。Slack 不通过 REST archive 路径推送
  `message_changed` / `message_deleted`。`make reconcile` 重拉最近 `RECONCILE_DAYS`（默认 90）
  天进新 session，splitter 按 `MAX(LOAD_DTTM)` dedup，最新版本胜出。每周跑一次。

## 路线图

- [x] v0.1 splitter MVP
- [x] v0.2 全 workspace archive
- [x] v0.3 静态 HTML（ref id + 排序）
- [x] v0.4 精细化渲染（mention / mrkdwn / unfurl / reactions popup / lightbox / fallback）
- [x] v0.5 编辑/删除兜底 `make reconcile`（90 天重拉 + LOAD_DTTM dedup）
- [x] v0.6 进度条（tqdm）+ 错误恢复（单元素失败不阻塞整体）+ pytest 套件 + package layout
- [ ] v1.0 定时任务（launchd / cron）
- [ ] v2.0 服务化（REST API + 搜索索引 + 多用户）

## 致谢

- [slackdump](https://github.com/rusq/slackdump) by Rustam Useldinov — 采集层。AGPLv3。
- [slack-export-viewer](https://github.com/hfaran/slack-export-viewer) — Jinja2 模板起点的 UI 参考。

## 协议

[MIT](LICENSE) © Xie Yanbo, 2026.

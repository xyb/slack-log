<sub><a href="README.md">🌐 English</a> · <b>中文</b></sub>

# slack-log

[![CI](https://github.com/xyb/slack-log/actions/workflows/ci.yml/badge.svg)](https://github.com/xyb/slack-log/actions/workflows/ci.yml)

把一个 Slack workspace 变成一个**可搜索的 web 服务**，带永久 ref-id 锚点——动态页面、
全文检索、可选 OIDC SSO；底层是一份**给 AI / shell `grep` 直接读的 JSONL 数据层**。
也能导出纯静态 HTML，用于没有后端的托管场景。

### 我为什么做这个

经常要翻老的 Slack thread —— 给新人补背景、写周报、找三个月前某个决策的原话。Slack
自家搜索免费版限制 1 万条，付费版也没法把一个 thread `grep` 出来或者喂给 AI。

现有工具都半截：[slackdump](https://github.com/rusq/slackdump) 把最难的部分（认证、限流、
增量 resume）做完了，但输出是 SQLite + 按日期分的 JSON，不是「一个 thread 一份文件」。
[slack-export-viewer](https://github.com/hfaran/slack-export-viewer) 是个 Flask
server，没有鉴权，也没有稳定的单条消息锚点。

所以 slack-log **站在 slackdump 上面**，只做 slackdump 不做的事：

1. 把 SQLite 切成一个 thread 一份 JSONL，文件名直接用 `thread_ts`（Slack 的稳定唯一 id）。
2. 动态提供页面——也能渲染静态 HTML——每条消息都有 `<a id="msg-{ts}">` 锚点，一个 URL
   贴到任何文档里，一年后还是指向同一条消息。
3. 全文检索（SQLite FTS5）、按人聚合的时间线、深浅色主题、中英文界面。
4. 附件按 mime / 大小阈值差异化下载（图片下，大 zip 只存 metadata）。
5. 把所有 uid / cid 解析成显示名，渲染 mrkdwn / 链接 unfurl 卡片 / reactions /
   lightbox，效果接近 Slack 原生。

### 你能得到什么

- **一个真正的 web 服务**。FastAPI server 直接从 JSONL 数据层动态渲染每个页面——频道
  列表、thread、按人聚合的时间线、附件——带 FTS5 全文检索。深浅色主题、中英文界面、
  时间按访问者自己浏览器的时区显示。可选 OIDC SSO，能安全地部署在登录后面。
- **……或者完全没有后端**。`make render-static` 出一份纯静态 HTML，`file://` 或任意
  静态服务都能开——同样的页面，相对链接。
- **一个 thread 一份纯 JSONL 文件**。`data/channels/<cid>/threads/<thread_ts>.jsonl`，
  Slack API 字段全保留（blocks / reactions / files / edited / attachments），每行一条
  完整消息。`grep` / `jq` / AI prompt 直接读。
- **永久 ref id**。每条消息 `<a id="msg-{ts}">`，URL 形如
  `…/threads/1779079280.797169#msg-1779154899.648009`，可以放心贴到任何文档当引用。
- **站在 slackdump 肩膀上**。认证 / 限流 / 增量 resume / thread reply 晚到检测——都交给
  slackdump，slack-log 只 subprocess 调它。
- **自动刷新**。server 自己跑数据刷新——一个按可配间隔运行的后台定时任务，外加按需触发的
  `POST /sync` API。
- **容器就绪**。一个公开的多架构 Docker 镜像；附带 Kubernetes 部署文件。

> 个人项目，MIT 协议。在一个 Slack workspace 上测过。Public / private channel /
> DM / MPIM 都能跑。

## 快速上手

### 本地起 web 服务

```sh
brew install slackdump
pip install -e .

# 给 slackdump 配 Slack 凭据（浏览器 cookie 拿 xoxc + xoxd）
# 最简方式见 https://github.com/rusq/slackdump/wiki/EZ-Login-3000
slackdump workspace new

make fetch && make split && make attach && make index
make serve            # → http://127.0.0.1:8770
```

### ……或导出静态 HTML

```sh
make render-static    # → html-static/，file:// 或任意静态服务都能开
```

### ……或跑容器

```sh
docker run -p 8770:8770 -v "$PWD/data:/data" xieyanbo/slack-log:0.9.0
```

### attach 的 Slack 凭据

`slackdump` 自己处理认证，但 `attach.py` 下载私有文件附件还需要你的 Slack 浏览器
token（`xoxc-...`）和 cookie（`xoxd-...`）。解析顺序：

1. 环境变量 `SLACK_XOXC` + `SLACK_XOXD`
2. 当前目录的 `./.env`（项目本地）
3. `~/.config/slack-log/.env`（用户级，遵循 XDG）

```sh
# 用户级 .env（记得 chmod 600）
mkdir -p ~/.config/slack-log
cat > ~/.config/slack-log/.env <<EOF
SLACK_XOXC=xoxc-your-token
SLACK_XOXD=xoxd-your-cookie
EOF
chmod 600 ~/.config/slack-log/.env
```

文件用 [python-dotenv](https://github.com/theskumar/python-dotenv) 解析标准 `.env` 格式。

## 部署

slack-log 以一个公开 Docker 镜像 + 一组 Kubernetes 部署文件的形式发布：

- **镜像**——Docker Hub 上的 `xieyanbo/slack-log`，多架构（amd64/arm64）。每次正式
  发布推一个固定 `X.Y.Z` 版本 tag；`:latest` 跟随最高版本号。部署仍固定到具体版本号。
- **Kubernetes**——`deploy/k8s/` 放脱敏的 `*.example.yaml`：Deployment + Service +
  Ingress + 一个共享 PVC。把 example 拷成去掉 `.example` 的同名文件，填进真实值再
  apply——真实那份不进 git。
- **刷新**——server 自己刷新数据：一个按 `SLACK_LOG_SYNC_INTERVAL` 间隔运行的后台
  定时任务，外加按需触发的 `POST /sync` API（bearer token 鉴权）。一把进程内锁保证两者
  不重叠——不需要单独的 CronJob。
- **OIDC SSO**——设了 `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` /
  `OIDC_DISCOVERY_URL` 三个环境变量，server 就要求登录；不设则开放运行，供本地开发。
  `/healthz` 永远公开。
- **CI/CD**——见 [docs/CICD.md](docs/CICD.md)。每次 push/PR 跑测试矩阵 + lint；
  打 `vX.Y.Z` tag 自动构建并发布镜像。

## 常用命令

```sh
make fetch               # 只跑 slackdump archive --resume（便宜，加性）
make split               # SQLite → 每 thread 一份 JSONL + users/channels
make attach              # 按 mime/size 策略差异化下载附件
make index               # 构建 search.db（FTS5 全文索引）
make serve               # 在 127.0.0.1:8770 起 web 服务
make render-static       # 导出静态 HTML flavor
make reconcile           # 重拉最近 90 天兜底编辑/删除（每周跑一次）
make help                # 看全部 target
```

## 架构

```
┌──────────────────────────────────────────────────┐
│  下游消费者：周报 / AI runbook / 历史检索         │
└──────────────────────────────────────────────────┘
                       ↑
┌──────────────────────────────────────────────────┐
│  服务层                                           │
│  FastAPI server — 动态页面 + FTS5 检索            │
│    + OIDC SSO + 深浅色 + 中英文                   │
│  静态 HTML 导出（render.py，无后端）              │
└──────────────────────────────────────────────────┘
                       ↑
┌──────────────────────────────────────────────────┐
│  数据层(双格式并存)                                │
│  thread JSONL  ← AI / grep / server              │
│  channel index.jsonl + users.json + channels.json│
│  search.db (FTS5)                                │
│  slackdump.sqlite ← SQL / 归档底本                │
│  attachments/(按阈值差异化下载)                    │
└──────────────────────────────────────────────────┘
                       ↑
┌──────────────────────────────────────────────────┐
│  采集层(slackdump subprocess)                     │
│  archive + Lookback resume + 限流 + 认证          │
└──────────────────────────────────────────────────┘
```

采集层完全外包给 slackdump（AGPLv3 binary，subprocess 调用——不污染 slack-log 的
MIT 协议）。

## 文件清单

| 路径 | 说明 |
|---|---|
| `slack_log/splitter.py` | `slackdump.sqlite` → 每 thread 一份 JSONL + `users.json` + `channels.json` |
| `slack_log/attach.py` | 扫 JSONL 的 files 字段，按 mime/size 策略差异化下载，每个文件都生成 `.meta.json` |
| `slack_log/indexer.py` | 构建 `search.db`——JSONL 之上的 SQLite FTS5 全文索引 |
| `slack_log/render.py` | 共享渲染函数；也是静态 HTML 导出器 |
| `slack_log/server.py` | FastAPI app——动态页面、检索、按人时间线、附件服务 |
| `slack_log/auth.py` | 可选 OIDC SSO 中间件 + 访问日志 |
| `slack_log/templates/` | `server/`（动态、绝对 URL）+ `static/`（相对 `.html`）两套 flavor |
| `deploy/k8s/` | 脱敏的 Kubernetes 部署文件（`*.example.yaml`） |
| `docs/CICD.md` | CI/CD 设计 + 运维参考文档 |
| `Makefile` | 构建 / 服务 target |

## 设计原则

- **默认动态**。server 直接从 JSONL 数据层渲染页面——没有预生成的 HTML 树，所以改模板
  立即生效，refresh 流水线也不需要 render 步骤。
- **附件跨 rebuild 保留**。`data/` 是宝贵的（重下载很慢），只有 `make clean-all` 才会删它。
- **稳定文件名**。Thread 文件用 `thread_ts`（Slack 唯一 id）命名——不是日期不是 preview，
  URL 永不变。
- **`.meta.json` 永远生成**。大 zip 和视频只存 metadata，原 Slack URL 保留，未来想下能下。
- **编辑/删除靠重拉兜底，不靠 event**。Slack 不通过 REST archive 路径推送
  `message_changed` / `message_deleted`。`make reconcile` 重拉最近 90 天，splitter 按
  `MAX(LOAD_DTTM)` dedup，最新版本胜出。每周跑一次。

## 路线图

- [x] v0.1–v0.6 —— splitter、全 workspace archive、带 ref id 的静态 HTML、精细化渲染、
  编辑/删除兜底、错误恢复 + 测试
- [x] v0.7 —— web 服务：HTTP 浏览 + FTS5 全文检索 + 按人时间线 + 深浅色 + 中英文 i18n
- [x] v0.8 —— OIDC SSO、Docker 镜像、Kubernetes 部署文件
- [x] v0.9 —— 全动态渲染、时间按浏览器时区显示、访问日志、GitHub Actions CI/CD

## 致谢

- [slackdump](https://github.com/rusq/slackdump) by Rustam Useldinov —— 采集层。AGPLv3。
- [slack-export-viewer](https://github.com/hfaran/slack-export-viewer) —— Jinja2 模板起点的 UI 参考。

## 协议

[MIT](LICENSE) © Xie Yanbo, 2026.

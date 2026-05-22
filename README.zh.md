<sub><a href="README.md">🌐 English</a> · <b>中文</b></sub>

# slack-log

[![CI](https://github.com/xyb/slack-log/actions/workflows/ci.yml/badge.svg)](https://github.com/xyb/slack-log/actions/workflows/ci.yml)

把一个 Slack workspace 变成能真正读、能 `grep`、能搜的东西——带永久 ref-id 锚点，一个
指向某条消息的链接一年后还有效。一份代码，**两种产品形态**：在自己电脑上跑，或者作为
团队 web 服务跑。

### 我为什么做这个

经常要翻老的 Slack thread——给新人补背景、写周报、找三个月前某个决策的原话。Slack
自家搜索免费版限制 1 万条，付费版也没法把一个 thread `grep` 出来或者喂给 AI。

slack-log **站在 [slackdump](https://github.com/rusq/slackdump) 上面**。slackdump 把最难
的部分做完了（认证、限流、增量 resume），slack-log 补上它不做的：一个 thread 一份
JSONL 的数据层、带稳定单条消息锚点的 web 服务、全文检索、接近 Slack 原生的渲染。

## 选择形态

slack-log 是一份代码、两种产品形态。按你的用法挑一个——区别只有一个
`SLACK_LOG_PROFILE` 开关。

|              | **个人版**                              | **团队版**                                |
|--------------|----------------------------------------|------------------------------------------|
| 运行环境     | 自己的电脑                              | 服务器                                    |
| 核心产物     | `data/` JSONL 数据层                    | `search.db`（单个 SQLite 文件）           |
| 用途         | `grep`、喂 AI、本地浏览                 | 团队共享、可搜索的 web 存档               |
| web 服务     | 本地，无登录                            | FastAPI + OIDC SSO                        |
| 刷新         | 手动（`make personal-build`）           | 内置定时任务 + `POST /sync`               |
| 部署         | —                                       | Docker 镜像 + Kubernetes 部署文件         |
| 指南         | [docs/personal.md](docs/personal.md)    | [docs/team.md](docs/team.md)              |

两种形态共用同一个采集层（slackdump）、同一套 FTS5 检索、同一套渲染——见
[docs/architecture.md](docs/architecture.md)。

## 个人版

自己 Slack 的本地存档。splitter 写出一个**机器友好的 JSONL 数据层**——一个 thread 一份
文件——`grep`、`jq`、AI prompt 直接读。本地 web 服务浏览它；静态 HTML 导出则完全不需要
后端。

```sh
brew install slackdump
pip install -e .

# 给 slackdump 配 Slack 凭据（浏览器 cookie 拿 xoxc + xoxd）
# 最简方式见 https://github.com/rusq/slackdump/wiki/EZ-Login-3000
slackdump workspace new

make personal-build      # slackdump archive → split → attach → index
make personal-serve      # → http://127.0.0.1:8770
```

每个 thread 是 `data/channels/<cid>/threads/<thread_ts>.jsonl`——Slack 字段全保留
（blocks / reactions / files / edited），每行一条完整消息。或者完全不用 server：

```sh
make render-static       # → html-static/，file:// 或任意静态服务都能开
```

完整说明——数据层、附件、编辑/删除兜底——见 [docs/personal.md](docs/personal.md)。

## 团队版

给团队的共享 web 存档。没有 JSONL 数据层：indexer 把 slackdump 的归档直接 ETL 进
`search.db`，server 只读这一个文件。FastAPI、OIDC SSO、进程内刷新定时任务、容器镜像。

```sh
docker run -p 8770:8770 \
  -e SLACK_LOG_PROFILE=team \
  -v "$PWD/data:/data" \
  xieyanbo/slack-log:0.11.0
```

真正的部署，`deploy/k8s/` 放了脱敏的 Kubernetes 部署文件（Deployment + Service +
Ingress + 一个共享 PVC），server 自己刷新自己——后台定时任务外加按需 `POST /sync`。
设了 `OIDC_*` 环境变量，OIDC SSO 立即开启。

完整部署指南——`search.db` schema、SSO、刷新、configmap——见 [docs/team.md](docs/team.md)。

## 两种形态都给你的

- **永久 ref id**。每条消息渲染成 `<a id="msg-{ts}">`。URL 形如
  `…/threads/1779079280.797169#msg-1779154899.648009`，可以放心贴到任何文档当引用。
- **全文检索**。SQLite FTS5，CJK 按单字切分，两个字的中文词也能精准命中。还有按人聚合
  的时间线。
- **接近原生的渲染**。uid/cid 解析成显示名，mrkdwn、链接 unfurl 卡片、reactions、图片
  lightbox——深浅色主题、中英文界面、时间按访问者自己的时区显示。
- **站在 slackdump 肩膀上**。认证、限流、增量 resume、thread reply 晚到检测——全交给
  slackdump，subprocess 调用（AGPLv3，不污染 slack-log 的 MIT 协议）。

> 个人项目，MIT 协议。在一个 Slack workspace 上测过。Public / private channel /
> DM / MPIM 都能跑。

## 架构

```
        slackdump archive  ─────────────►  raw/slackdump.sqlite
                                                   │
              ┌────────────────────────────────────┴───────────────┐
            个人版                                                 团队版
              │                                                     │
        splitter → data/ jsonl                          indexer ETL ─┘
              │            │                                     │
        attach (附件)    indexer                                  ▼
              │            │                                  search.db
              ▼            ▼                          (messages + message_raw
        data/ + search.db                              + threads + channels
              │                                              + users)
              ▼                                                  │
        JsonlStore ─────────►  ArchiveStore  ◄───────── SqliteStore
                                     │
                                FastAPI server
```

server 只依赖 `ArchiveStore`；`JsonlStore` 和 `SqliteStore` 是两个后端。设计细节——store
抽象、`core/` 共享层、扩展后的 `search.db` schema——见
[docs/architecture.md](docs/architecture.md)。

## 文件清单

| 路径 | 说明 |
|---|---|
| `slack_log/core/` | 共享层——`slackdump_db`（读归档 SQLite）+ `text`（Slack 文本处理）|
| `slack_log/store/` | `ArchiveStore` 抽象 + `JsonlStore`（个人）/ `SqliteStore`（团队）|
| `slack_log/config.py` | `Profile` 枚举 + `Config.from_env` |
| `slack_log/pipeline/` | 数据处理——`split` · `attach` · `index` |
| `slack_log/web/` | 服务层——`app`（FastAPI）· `presenter` · `static_export` · `auth` · `sync` |
| `deploy/k8s/` | 脱敏的 Kubernetes 部署文件（`*.example.yaml`）|
| `docs/` | `personal.md` · `team.md` · `architecture.md` · `CICD.md` |

## 路线图

- [x] v0.1–v0.6 —— splitter、全 workspace archive、带 ref id 的静态 HTML、精细化渲染、
  编辑/删除兜底、错误恢复 + 测试
- [x] v0.7 —— web 服务：HTTP 浏览 + FTS5 全文检索 + 按人时间线
- [x] v0.8 —— OIDC SSO、Docker 镜像、Kubernetes 部署文件
- [x] v0.9 —— 全动态渲染、时间按浏览器时区显示、CI/CD
- [x] v0.10 —— 服务内刷新、splitter 重写（N+1 → 三遍线性扫）
- [x] v0.11 —— 个人版 / 团队版形态二分（`ArchiveStore` 抽象、两个后端、一个
  `SLACK_LOG_PROFILE` 开关）；团队版附件下载 + 可配置大小上限；增量刷新
  （`slackdump resume`）

## 致谢

- [slackdump](https://github.com/rusq/slackdump) by Rustam Useldinov —— 采集层。AGPLv3。
- [slack-export-viewer](https://github.com/hfaran/slack-export-viewer) —— Jinja2 模板起点的 UI 参考。

## 协议

[MIT](LICENSE) © Xie Yanbo, 2026.

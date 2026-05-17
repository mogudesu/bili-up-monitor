<div align="center">

# 📺 Bili UP Monitor

**B站UP主动态监控 · 飞书实时推送 · 开箱即用**

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://python.org)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Android-green.svg)]()
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![AI Generated](https://img.shields.io/badge/Code%20%26%20Docs-AI%20Generated-9cf.svg)]()

> 🤖 **本项目代码与文档均由 AI 生成**，包括全部源码、构建脚本、README 文档等。

[功能特性](#-功能特性) · [快速开始](#-快速开始) · [下载安装](#-下载安装) · [配置说明](#-配置说明) · [构建](#-构建)

</div>

---

## ✨ 功能特性

### 🔍 全方位监控

| | 功能 | 说明 |
|---|------|------|
| 🎬 | **视频检测** | 自动检测UP主新发布的视频，获取标题、简介、播放量、封面 |
| 🖼️ | **图文动态** | 完整捕获图文动态的文字内容和图片，推送至飞书 |
| 📝 | **文字动态** | 支持纯文字动态、转发动态等多种动态类型 |
| ⏱️ | **灵活间隔** | 30s / 1m / 5m / 10m / 15m / 30m / 1h ~ 24h 自由选择 |
| 🔄 | **自动重试** | 推送失败的消息下次检查时自动重试，不遗漏 |
| 🔙 | **24h回溯** | 首次运行自动回溯24小时内的更新 |

### 📢 飞书推送

| | 功能 | 说明 |
|---|------|------|
| 🤖 | **Webhook推送** | 通过飞书自定义机器人 Webhook 发送通知 |
| 🃏 | **交互卡片** | 精美的飞书交互卡片格式，标题 + 内容 + 图片 + 跳转按钮 |
| 🖼️ | **图片推送** | 自动上传B站封面/动态图片到飞书，App认证支持 |
| 🔀 | **多群组路由** | 不同UP主推送到不同飞书群组，灵活分配 |
| 🔐 | **签名验证** | 支持 Webhook 签名安全验证 |
| 📊 | **摘要通知** | 每次检查完成后发送汇总摘要 |

### 🖥️ 可视化控制面板

基于 Flask 的 Web 控制面板，无需命令行操作：

> 📊 状态总览 · 🎯 UP主管理 · ▶️ 监控控制 · ⚙️ 快捷配置 · 📋 记录查看 · 📜 实时日志 · 🍪 Cookie管理

### 📱 多平台支持

| 平台 | 形态 | 说明 |
|------|------|------|
| 🪟 Windows | EXE 桌面应用 | 双击运行，无控制台窗口，原生窗口体验 |
| 🤖 Android | APK 手机应用 | 后台服务运行，随时随地监控 |
| 💻 CLI | 命令行 | 纯命令行运行，适合服务器部署 |

---

## 📥 下载安装

前往 [Releases](../../releases) 页面下载最新版本：

| 文件 | 平台 | 说明 |
|------|------|------|
| `MOGU-bili监控器.exe` | Windows | 双击即可运行，无需安装 Python |
| `bilibilimonitor-*.apk` | Android | 安装到手机，配置后自动后台监控 |

> 💡 首次运行 Windows 版如遇安全提示，请点击「更多信息」→「仍要运行」

---

## 🚀 快速开始

### 方式一：直接运行（免安装）

1. 从 [Releases](../../releases) 下载对应平台的安装包
2. Windows 双击 EXE 运行；Android 安装 APK
3. 在控制面板中配置监控目标和飞书 Webhook
4. 点击「启动监控」

### 方式二：从源码运行

```bash
# 克隆仓库
git clone https://github.com/mogugu555/bili-up-monitor.git
cd bili-up-monitor

# 安装依赖
pip install -r requirements.txt

# 配置
cp .env.example .env
# 编辑 .env 填写监控目标和飞书配置

# 启动监控
python src/main.py

# 或启动控制面板
python src/web_server.py
```

### 命令行参数

```bash
python src/main.py [选项]

选项:
  --once              单次检查后退出
  --uid UID           临时覆盖监控UID列表
  --interval 5m       临时覆盖监控间隔
  --add UID           添加临时监控UID
  --dry-run           试运行，不发送通知
  --check-unnotified  查看未通知的记录
  --cleanup           清理过期数据
```

---

## ⚙️ 配置说明

编辑 `.env` 文件进行配置（首次运行自动生成模板）：

```env
# ========== 监控目标 ==========
# 逗号分隔的 UID 列表
BILIBILI_TARGET_UIDS=21424587,1309819

# ========== 监控间隔 ==========
# 支持: 30s, 1m, 5m, 10m, 15m, 30m, 1h, 2h, 6h, 12h, 24h
MONITOR_INTERVAL=5m

# ========== 飞书推送 ==========
ENABLE_FEISHU=true
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx
FEISHU_WEBHOOK_SECRET=your-secret

# 飞书App认证（推送图片必需）
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx

# ========== 本地输出 ==========
ENABLE_LOCAL_OUTPUT=true
LOCAL_OUTPUT_DIR=data/output

# ========== 视频下载 ==========
ENABLE_VIDEO_DOWNLOAD=false
VIDEO_DOWNLOAD_DIR=data/videos
VIDEO_DOWNLOAD_MODE=analysis-fast

# ========== 通用 ==========
RETENTION_DAYS=30
LOG_LEVEL=INFO
```

### 飞书机器人配置

1. 在飞书群聊中添加「自定义机器人」，获取 Webhook URL 和签名密钥
2. 将 URL 填入 `FEISHU_WEBHOOK_URL`，密钥填入 `FEISHU_WEBHOOK_SECRET`
3. 如需推送图片，需创建飞书应用并填写 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`
4. 可通过控制面板的「飞书机器人分组」为不同UP主配置不同的飞书群组

---

## 🏗️ 项目架构

```
bilibili-monitor/
├── src/
│   ├── app.pyw          # Windows 桌面应用入口
│   ├── main.py          # Android / CLI 入口
│   ├── web_server.py    # Web 控制面板（Flask）
│   ├── dashboard.html   # 控制面板前端
│   ├── config.py        # 配置加载
│   ├── fetcher.py       # B站数据获取（公开API + WBI签名）
│   ├── store.py         # SQLite 状态存储
│   ├── monitor.py       # 监控主逻辑
│   └── notifier.py      # 通知推送（飞书 + 本地文件）
├── build_apk.py         # APK 构建脚本
├── build_exe.py         # EXE 构建脚本
├── buildozer.spec       # Buildozer 配置
├── .env.example         # 配置模板
└── requirements.txt     # Python 依赖
```

### 数据流

```
B站公开API ──→ fetcher.py ──→ monitor.py ──→ store.py (SQLite去重)
                                       │
                                       ├──→ notifier.py ──→ 飞书Webhook
                                       │                  ──→ 本地Markdown
                                       │
                                       └──→ web_server.py ──→ 可视化控制面板
```

### 数据获取策略

采用多级回退策略，无需 Cookie 即可运行：

1. **Cookie动态API**（优先）：使用 SESSDATA 获取完整动态数据
2. **公开Opus Feed**（回退）：WBI签名的 `opus/feed/space` 探测动态
3. **视频搜索API**：`x/space/wbi/arc/search` 获取视频列表
4. **视频详情API**：`x/web-interface/view` 获取视频元数据
5. **关键词搜索**：视频标题关键词搜索关联的转发动态

---

## 🔨 构建

### Windows EXE

```bash
pip install pyinstaller
python build_exe.py
# 输出: dist/MOGU-bili监控器.exe
```

### Android APK

需要 WSL (Ubuntu) + buildozer 环境：

```bash
python build_apk.py
# 输出: dist/bilibilimonitor-1.0.0-arm64-v8a-debug.apk
```

---

## 📋 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.12+ |
| 数据库 | SQLite3 |
| Web框架 | Flask |
| 桌面GUI | pywebview |
| 移动端 | Kivy + python-for-android |
| Windows打包 | PyInstaller |
| Android打包 | Buildozer |
| HTTP | urllib (零外部依赖核心) / requests |

---

## ⚠️ 注意事项

- 首次运行默认回溯24小时内的更新
- 监控间隔不建议低于1分钟，避免触发B站反爬机制
- 飞书 Webhook 有频率限制，建议间隔不低于5分钟
- 过期记录自动清理，默认保留30天
- 推送图片需配置飞书 App ID / App Secret

---

## 📄 License

[MIT License](LICENSE)

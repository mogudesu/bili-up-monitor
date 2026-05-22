<div align="center">

# 📺 Bili UP Monitor

**Bilibili UP Dynamic Monitor · Feishu Real-time Push · Ready to Use**

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://python.org)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Android-green.svg)]()
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![AI Generated](https://img.shields.io/badge/Code%20%26%20Docs-AI%20Generated-9cf.svg)]()

> 🤖 **All code and documentation in this project are AI-generated**, including source code, build scripts, README, etc.

[Features](#-features) · [Quick Start](#-quick-start) · [Download](#-download) · [Configuration](#-configuration) · [Build](#-build)

</div>

---

## ✨ Features

### 🔍 Comprehensive Monitoring

| | Feature | Description |
|---|---------|-------------|
| 🎬 | **Video Detection** | Automatically detect new videos from UP hosts, fetch title, description, views, cover |
| 🖼️ | **Image-Text Dynamics** | Capture image-text dynamics with full text content and images, push to Feishu |
| 📝 | **Text Dynamics** | Support pure text dynamics, repost dynamics and other dynamic types |
| ⏱️ | **Flexible Interval** | 30s / 1m / 5m / 10m / 15m / 30m / 1h ~ 24h configurable |
| 🔄 | **Auto Retry** | Failed push messages are automatically retried on next check, no misses |
| 🔙 | **24h Lookback** | First run automatically looks back 24 hours of updates |

### 📢 Feishu Push

| | Feature | Description |
|---|---------|-------------|
| 🤖 | **Webhook Push** | Send notifications via Feishu custom bot Webhook |
| 🃏 | **Interactive Card** | Beautiful Feishu interactive card format with title + content + image + jump button |
| 🖼️ | **Image Push** | Automatically upload Bilibili covers/dynamic images to Feishu, App auth supported |
| 🔀 | **Multi-Group Routing** | Push different UP hosts to different Feishu groups, flexible assignment |
| 🔐 | **Signature Verification** | Support Webhook signature security verification |
| 📊 | **Summary Notification** | Send summary notification after each check cycle |

### 🖥️ Visual Dashboard

Flask-based web dashboard, no command line needed:

> 📊 Status Overview · 🎯 UP Host Management · ▶️ Monitor Control · ⚙️ Quick Config · 📋 Record View · 📜 Real-time Logs · 🍪 Cookie Management

### 📱 Multi-Platform Support

| Platform | Form | Description |
|----------|------|-------------|
| 🪟 Windows | EXE Desktop App | Double-click to run, no console window, native window experience |
| 🤖 Android | APK Mobile App | Background service, monitor anytime anywhere |
| 💻 CLI | Command Line | Pure command line, suitable for server deployment |

---

## 📥 Download

Go to the [Releases](../../releases) page to download the latest version:

| File | Platform | Description |
|------|----------|-------------|
| `MOGU-bili监控器.exe` | Windows | Double-click to run, no Python installation needed |
| `bilibilimonitor-*.apk` | Android | Install on phone, auto background monitoring after config |

> 💡 If Windows shows a security warning on first run, click "More info" → "Run anyway"

---

## 🚀 Quick Start

### Option 1: Direct Run (No Installation)

1. Download the installer for your platform from [Releases](../../releases)
2. Windows: double-click the EXE; Android: install the APK
3. Configure monitoring targets and Feishu Webhook in the dashboard
4. Click "Start Monitoring"

### Option 2: Run from Source

```bash
# Clone the repository
git clone https://github.com/mogudesu/bili-up-monitor.git
cd bili-up-monitor

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env to fill in monitoring targets and Feishu config

# Start monitoring
python src/main.py

# Or start the dashboard
python src/web_server.py
```

### Command Line Arguments

```bash
python src/main.py [options]

Options:
  --once              Run a single check and exit
  --uid UID           Temporarily override monitoring UID list
  --interval 5m       Temporarily override monitoring interval
  --add UID           Add a temporary monitoring UID
  --dry-run           Trial run, do not send notifications
  --check-unnotified  View unnotified records
  --cleanup           Clean up expired data
```

---

## ⚙️ Configuration

Edit the `.env` file for configuration (auto-generated on first run):

```env
# ========== Monitoring Targets ==========
# Comma-separated UID list
BILIBILI_TARGET_UIDS=21424587,1309819

# ========== Monitoring Interval ==========
# Supported: 30s, 1m, 5m, 10m, 15m, 30m, 1h, 2h, 6h, 12h, 24h
MONITOR_INTERVAL=5m

# ========== Feishu Push ==========
ENABLE_FEISHU=true
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx
FEISHU_WEBHOOK_SECRET=your-secret

# Feishu App Auth (required for image push)
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx

# ========== Local Output ==========
ENABLE_LOCAL_OUTPUT=true
LOCAL_OUTPUT_DIR=data/output

# ========== Video Download ==========
ENABLE_VIDEO_DOWNLOAD=false
VIDEO_DOWNLOAD_DIR=data/videos
VIDEO_DOWNLOAD_MODE=analysis-fast

# ========== General ==========
RETENTION_DAYS=30
LOG_LEVEL=INFO
```

### Feishu Bot Configuration

1. Add a "Custom Bot" in Feishu group chat, get the Webhook URL and signing secret
2. Fill the URL in `FEISHU_WEBHOOK_URL`, the secret in `FEISHU_WEBHOOK_SECRET`
3. To push images, create a Feishu App and fill in `FEISHU_APP_ID` and `FEISHU_APP_SECRET`
4. Use the "Feishu Bot Groups" feature in the dashboard to assign different Feishu groups for different UP hosts

---

## 🏗️ Project Architecture

```
bilibili-monitor/
├── src/
│   ├── app.pyw          # Windows desktop app entry
│   ├── main.py          # Android / CLI entry
│   ├── web_server.py    # Web dashboard (Flask)
│   ├── dashboard.html   # Dashboard frontend
│   ├── config.py        # Configuration loader
│   ├── fetcher.py       # Bilibili data fetching (public API + WBI signing)
│   ├── store.py         # SQLite state storage
│   ├── monitor.py       # Main monitoring logic
│   └── notifier.py      # Notification push (Feishu + local file)
├── build_apk.py         # APK build script
├── build_exe.py         # EXE build script
├── buildozer.spec       # Buildozer configuration
├── .env.example         # Configuration template
└── requirements.txt     # Python dependencies
```

### Data Flow

```
Bilibili Public API ──→ fetcher.py ──→ monitor.py ──→ store.py (SQLite dedup)
                                            │
                                            ├──→ notifier.py ──→ Feishu Webhook
                                            │                  ──→ Local Markdown
                                            │
                                            └──→ web_server.py ──→ Visual Dashboard
```

### Data Fetching Strategy

Multi-level fallback strategy, works without cookies:

1. **Cookie Dynamic API** (preferred): Use SESSDATA for complete dynamic data
2. **Public Opus Feed** (fallback): WBI-signed `opus/feed/space` to probe dynamics
3. **Video Search API**: `x/space/wbi/arc/search` for video list
4. **Video Detail API**: `x/web-interface/view` for video metadata
5. **Keyword Search**: Video title keyword search for linked repost dynamics

---

## 🔨 Build

### Windows EXE

```bash
pip install pyinstaller
python build_exe.py
# Output: dist/MOGU-bili监控器.exe
```

### Android APK

Requires WSL (Ubuntu) + buildozer environment:

```bash
python build_apk.py
# Output: dist/bilibilimonitor-1.0.0-arm64-v8a-debug.apk
```

---

## 📋 Tech Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.12+ |
| Database | SQLite3 |
| Web Framework | Flask |
| Desktop GUI | pywebview |
| Mobile | Kivy + python-for-android |
| Windows Packaging | PyInstaller |
| Android Packaging | Buildozer |
| HTTP | urllib (zero external dependency core) / requests |

---

## ⚠️ Notes

- First run defaults to looking back 24 hours of updates
- Monitoring interval should not be less than 1 minute to avoid triggering Bilibili anti-crawling
- Feishu Webhook has rate limits, recommended interval not less than 5 minutes
- Expired records are automatically cleaned up, default retention is 30 days
- Image push requires Feishu App ID / App Secret configuration

---

## 📄 License

[MIT License](LICENSE)

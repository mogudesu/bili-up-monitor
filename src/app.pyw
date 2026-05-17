#!/usr/bin/env python3
"""Bilibili UP Monitor - Desktop Application (GUI mode, no console)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import queue
from pathlib import Path

# CRITICAL: Detach from console on Windows BEFORE anything else
if sys.platform == "win32":
    try:
        import ctypes
        # Free the console completely - this prevents any CMD window from appearing
        ctypes.windll.kernel32.FreeConsole()
        # Also hide if a console window somehow exists
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass

# Resolve paths
if getattr(sys, "frozen", False):
    _EXE_DIR = Path(sys.executable).parent
    _BUNDLE_DIR = Path(sys._MEIPASS)
    sys.path.insert(0, str(_BUNDLE_DIR))
else:
    _EXE_DIR = Path(__file__).parent.parent
    _BUNDLE_DIR = Path(__file__).parent
    sys.path.insert(0, str(_BUNDLE_DIR))

from config import load_config
from web_server import create_app, setup_logging

# Global log queue for built-in terminal
log_queue = queue.Queue()


class QueueHandler(logging.Handler):
    """Send log records to a queue for the UI terminal."""

    def emit(self, record):
        try:
            msg = self.format(record)
            log_queue.put(msg)
        except Exception:
            pass


def setup_queue_logging():
    """Add queue handler to root logger so web_server logs appear in UI."""
    handler = QueueHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(handler)


def hide_console():
    """Free and hide console window on Windows."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.FreeConsole()
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)
        except Exception:
            pass


def main() -> None:
    hide_console()

    parser = argparse.ArgumentParser(description="Bilibili UP Monitor - Desktop App")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8199, help="Port to bind (default: 8199)")
    parser.add_argument("--env", default=".env", help="Env file name (default: .env)")
    args = parser.parse_args()

    project_root = _EXE_DIR
    env_file = project_root / args.env

    if not env_file.exists():
        default_env = """# Bilibili UP Monitor Configuration
# Auto-generated on first run - edit as needed

# ========== Monitoring ==========
BILIBILI_TARGET_UIDS=
MONITOR_INTERVAL=5m

# ========== Database ==========
DB_PATH=data/monitor.db

# ========== Feishu ==========
ENABLE_FEISHU=false
FEISHU_WEBHOOK_URL=
FEISHU_WEBHOOK_SECRET=
FEISHU_APP_ID=
FEISHU_APP_SECRET=

# ========== Local Output ==========
ENABLE_LOCAL_OUTPUT=true
LOCAL_OUTPUT_DIR=data/output

# ========== Video Download ==========
ENABLE_VIDEO_DOWNLOAD=false
VIDEO_DOWNLOAD_DIR=data/videos
VIDEO_DOWNLOAD_MODE=analysis-fast

# ========== General ==========
TIMEZONE_OFFSET=-480
RETENTION_DAYS=30
LOG_LEVEL=INFO
"""
        env_file.write_text(default_env, encoding="utf-8")

    config = load_config(str(env_file))
    config.base_dir = project_root
    setup_logging(config)
    setup_queue_logging()

    app = create_app(config)

    from web_server import state

    for uid in config.target_uids:
        state.get_user_info(uid)

    # Start Flask in a background thread
    flask_thread = threading.Thread(
        target=lambda: app.run(
            host=args.host, port=args.port, debug=False, threaded=True, use_reloader=False
        ),
        daemon=True,
    )
    flask_thread.start()

    # Wait for Flask to be ready
    import urllib.request
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{args.port}/")
            break
        except Exception:
            time.sleep(0.3)

    # Launch pywebview window
    import webview

    url = f"http://127.0.0.1:{args.port}/"

    class Api:
        def get_version(self):
            return "1.0.0"

        def get_logs(self):
            """Fetch pending log lines for the built-in terminal."""
            lines = []
            try:
                while True:
                    lines.append(log_queue.get_nowait())
            except queue.Empty:
                pass
            return lines

    api = Api()

    window = webview.create_window(
        title="MOGU-bili监控器",
        url=url,
        js_api=api,
        width=1400,
        height=900,
        min_size=(900, 600),
        text_select=True,
    )

    def on_closing():
        try:
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.user32.PostQuitMessage(0)
        except Exception:
            pass
        os._exit(0)

    window.events.closing += on_closing

    webview.start(debug=False)
    sys.exit(0)


if __name__ == "__main__":
    main()

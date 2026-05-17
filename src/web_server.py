#!/usr/bin/env python3
"""Bilibili UP Monitor - Web Dashboard Server.

Provides a visual dashboard for monitoring control and configuration.

Usage:
    py -3 src/web_server.py              # Start dashboard on http://localhost:8199
    py -3 src/web_server.py --port 9000  # Custom port
    py -3 src/web_server.py --debug      # Debug mode
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

# Add src directory to path
if getattr(sys, 'frozen', False):
    # PyInstaller frozen mode: src modules are bundled in _MEIPASS
    _BUNDLE_DIR = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    _EXE_DIR = Path(sys.executable).parent
    sys.path.insert(0, str(_BUNDLE_DIR))
else:
    _BUNDLE_DIR = Path(__file__).parent
    _EXE_DIR = Path(__file__).parent.parent
    sys.path.insert(0, str(_BUNDLE_DIR))

from config import load_config, MonitorConfig, _parse_interval
from store import MonitorStore, normalize_bilibili_uid
from fetcher import fetch_user_info, fetch_up_updates, set_cookies, get_cookies, clear_cookies, parse_cookie_string, check_cookie_status
from monitor import BilibiliMonitor, setup_logging

# ---- Flask App ----

try:
    from flask import Flask, jsonify, request, send_from_directory
except ImportError:
    print("Flask is required. Install with: pip install flask")
    print("  pip install flask")
    sys.exit(1)

app = Flask(__name__, static_folder=None)
logger = logging.getLogger("bilibili-monitor.web")

TRANSFER_ENV_KEYS = {
    "BILIBILI_TARGET_UIDS", "MONITOR_INTERVAL", "ENABLE_FEISHU",
    "FEISHU_WEBHOOK_URL", "FEISHU_WEBHOOK_SECRET", "FEISHU_APP_ID", "FEISHU_APP_SECRET",
    "BILIBILI_COOKIE",
    "ENABLE_LOCAL_OUTPUT", "ENABLE_VIDEO_DOWNLOAD", "VIDEO_DOWNLOAD_MODE",
    "TIMEZONE_OFFSET", "RETENTION_DAYS", "LOG_LEVEL",
}

# ---- Global State ----

class MonitorState:
    """Shared mutable state for the web dashboard."""

    def __init__(self, config: MonitorConfig):
        self.config = config
        self.db_path = str(config.resolve_path(config.db_path))
        self._monitor: Optional[BilibiliMonitor] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._running = False
        self._process: Optional[subprocess.Popen] = None
        self._check_history: list[dict[str, Any]] = []
        self._log_lines: list[str] = []
        self._max_log_lines = 500
        self._user_cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._db_local = threading.local()

    def _get_db(self) -> sqlite3.Connection:
        """Get a thread-local database connection."""
        import sqlite3
        conn = getattr(self._db_local, 'conn', None)
        if conn is None:
            db_dir = Path(self.db_path).parent
            db_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            from store import CREATE_TABLES_SQL
            conn.executescript(CREATE_TABLES_SQL)
            self._db_local.conn = conn
        return conn

    def get_user_info(self, uid: str) -> dict[str, Any]:
        """Get user info with caching."""
        if uid not in self._user_cache:
            try:
                self._user_cache[uid] = fetch_user_info(uid)
            except Exception:
                self._user_cache[uid] = {
                    "uid": uid,
                    "name": f"UID:{uid}",
                    "face": None,
                    "space_url": f"https://space.bilibili.com/{uid}",
                }
        return self._user_cache[uid]

    def add_log(self, line: str):
        """Add a log line to the circular buffer."""
        with self._lock:
            self._log_lines.append(line)
            if len(self._log_lines) > self._max_log_lines:
                self._log_lines = self._log_lines[-self._max_log_lines:]

    def get_logs(self, after: int = 0) -> list[str]:
        """Get log lines after index."""
        with self._lock:
            return self._log_lines[after:]

    def add_check_result(self, results: list[dict[str, Any]]):
        """Record a check result to history."""
        now = datetime.now(timezone(timedelta(hours=8)))
        entry = {
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "ts": time.time(),
            "results": results,
            "new_videos": sum(len(r.get("new_videos", [])) for r in results),
            "new_dynamics": sum(len(r.get("new_dynamics", [])) for r in results),
            "retried_videos": sum(len(r.get("retried_videos", [])) for r in results),
            "retried_dynamics": sum(len(r.get("retried_dynamics", [])) for r in results),
            "errors": sum(1 for r in results if r.get("error")),
        }
        with self._lock:
            self._check_history.insert(0, entry)
            if len(self._check_history) > 100:
                self._check_history = self._check_history[:100]
        try:
            conn = self._get_db()
            summary_parts = []
            for r in results:
                name = r.get("author_name", "")
                nv = len(r.get("new_videos", []))
                nd = len(r.get("new_dynamics", []))
                if nv > 0 or nd > 0:
                    summary_parts.append(f"{name}:{nv}v{nd}d")
            conn.execute(
                "INSERT INTO check_history (timestamp, ts, new_videos, new_dynamics, errors, summary) VALUES (?, ?, ?, ?, ?, ?)",
                (entry["timestamp"], entry["ts"], entry["new_videos"], entry["new_dynamics"], entry["errors"], "; ".join(summary_parts)),
            )
            conn.commit()
        except Exception:
            pass
        return entry

    def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            if self._check_history:
                return self._check_history[:limit]
        try:
            conn = self._get_db()
            rows = conn.execute(
                "SELECT timestamp, ts, new_videos, new_dynamics, errors, summary FROM check_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]
        except Exception:
            pass
        return []


state: Optional[MonitorState] = None


# ---- Config Persistence ----

def _env_file_path() -> Path:
    """Return the .env file path (next to the exe or project root)."""
    return _EXE_DIR / ".env"


def _get_local_ips() -> list[str]:
    """Get local network IP addresses."""
    import socket
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addr = info[4][0]
            if addr != "127.0.0.1" and addr not in ips:
                ips.append(addr)
    except Exception:
        pass
    return ips


def _read_env_file() -> dict[str, str]:
    """Read .env file into a dict."""
    env_path = _env_file_path()
    result = {}
    if not env_path.exists():
        return result
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def _write_env_file(env_dict: dict[str, str]) -> None:
    """Write dict back to .env file, preserving comments and order."""
    env_path = _env_file_path()
    example_path = _EXE_DIR / ".env.example"

    # Build ordered output
    lines_out: list[str] = []
    written_keys: set[str] = set()

    # Read existing file to preserve structure
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                lines_out.append(line)
                continue
            if "=" in stripped:
                key, _, _ = stripped.partition("=")
                key = key.strip()
                if key in env_dict:
                    lines_out.append(f"{key}={env_dict[key]}")
                    written_keys.add(key)
                else:
                    lines_out.append(line)
            else:
                lines_out.append(line)

    # Add any new keys not in the file
    for key, value in env_dict.items():
        if key not in written_keys:
            lines_out.append(f"{key}={value}")

    env_path.write_text("\n".join(lines_out) + "\n", encoding="utf-8")


def _parse_target_uids(raw: str) -> list[str]:
    uids: list[str] = []
    for token in str(raw or "").split(","):
        uid = normalize_bilibili_uid(token)
        if uid and uid.isdigit() and uid not in uids:
            uids.append(uid)
    return uids


def _apply_config_to_runtime(updated: dict[str, Any]) -> None:
    if "BILIBILI_TARGET_UIDS" in updated:
        state.config.target_uids = _parse_target_uids(str(updated["BILIBILI_TARGET_UIDS"]))
        state._user_cache.clear()
    if "MONITOR_INTERVAL" in updated:
        try:
            state.config.interval_seconds = _parse_interval(str(updated["MONITOR_INTERVAL"]))
        except ValueError:
            pass
    if "ENABLE_FEISHU" in updated:
        state.config.enable_feishu = str(updated["ENABLE_FEISHU"]).lower() in ("true", "1", "yes")
    if "FEISHU_WEBHOOK_URL" in updated:
        state.config.feishu_webhook_url = str(updated["FEISHU_WEBHOOK_URL"])
    if "FEISHU_WEBHOOK_SECRET" in updated:
        state.config.feishu_webhook_secret = str(updated["FEISHU_WEBHOOK_SECRET"])
    if "FEISHU_APP_ID" in updated:
        state.config.feishu_app_id = str(updated["FEISHU_APP_ID"])
    if "FEISHU_APP_SECRET" in updated:
        state.config.feishu_app_secret = str(updated["FEISHU_APP_SECRET"])
    if "BILIBILI_COOKIE" in updated:
        cookie_dict = parse_cookie_string(str(updated["BILIBILI_COOKIE"]))
        if cookie_dict:
            set_cookies(cookie_dict)
        else:
            clear_cookies()
    if "ENABLE_LOCAL_OUTPUT" in updated:
        state.config.enable_local_output = str(updated["ENABLE_LOCAL_OUTPUT"]).lower() in ("true", "1", "yes")
    if "ENABLE_VIDEO_DOWNLOAD" in updated:
        state.config.enable_video_download = str(updated["ENABLE_VIDEO_DOWNLOAD"]).lower() in ("true", "1", "yes")
    if "VIDEO_DOWNLOAD_MODE" in updated:
        state.config.video_download_mode = str(updated["VIDEO_DOWNLOAD_MODE"])
    if "RETENTION_DAYS" in updated:
        try:
            state.config.retention_days = int(updated["RETENTION_DAYS"])
        except ValueError:
            pass
    if "LOG_LEVEL" in updated:
        state.config.log_level = str(updated["LOG_LEVEL"])


def _find_recent_image_url() -> str:
    conn = state._get_db()
    row = conn.execute(
        "SELECT pic FROM seen_videos WHERE pic IS NOT NULL AND pic != '' ORDER BY first_seen_ts DESC LIMIT 1"
    ).fetchone()
    if row and row["pic"]:
        return row["pic"]
    rows = conn.execute(
        "SELECT image_urls FROM seen_dynamics WHERE image_urls IS NOT NULL AND image_urls != '' ORDER BY first_seen_ts DESC LIMIT 20"
    ).fetchall()
    for row in rows:
        try:
            urls = json.loads(row["image_urls"] or "[]")
        except Exception:
            urls = []
        if urls:
            return str(urls[0])
    return ""


# ---- API Routes ----

@app.route("/")
def index():
    """Serve the dashboard HTML."""
    if getattr(sys, 'frozen', False):
        dashboard_dir = _BUNDLE_DIR
    else:
        dashboard_dir = Path(__file__).parent
    return send_from_directory(str(dashboard_dir), "dashboard.html")


@app.route("/manifest.json")
def serve_manifest():
    """Serve PWA manifest."""
    if getattr(sys, 'frozen', False):
        dashboard_dir = _BUNDLE_DIR
    else:
        dashboard_dir = Path(__file__).parent
    return send_from_directory(str(dashboard_dir), "manifest.json", mimetype="application/json")


@app.route("/icon.png")
def serve_icon_png():
    """Serve the main app icon."""
    if getattr(sys, 'frozen', False):
        icon_dir = _BUNDLE_DIR
    else:
        icon_dir = Path(__file__).parent.parent
    icon_path = icon_dir / "icon.png"
    if icon_path.exists():
        return send_from_directory(str(icon_dir), "icon.png", mimetype="image/png")
    return "", 404


@app.route("/icon-<size>.png")
def serve_icon(size):
    """Serve PWA icon (generated on-the-fly as minimal PNG if missing)."""
    import struct
    import zlib

    icon_dir = _EXE_DIR / "data"
    icon_dir.mkdir(parents=True, exist_ok=True)
    icon_path = icon_dir / f"icon-{size}.png"
    if icon_path.exists():
        return send_from_directory(str(icon_dir), f"icon-{size}.png", mimetype="image/png")

    img_size = int(size)
    bg = (15, 15, 26)
    fg = (251, 114, 153)
    cx, cy, r = img_size // 2, img_size // 2, img_size // 4

    raw_rows = []
    for y in range(img_size):
        row = b"\x00"
        for x in range(img_size):
            dx, dy = x - cx, y - cy
            if dx * dx + dy * dy <= r * r:
                row += bytes(fg)
            else:
                row += bytes(bg)
        raw_rows.append(row)

    raw_data = b"".join(raw_rows)

    def _png_chunk(chunk_type, data):
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", img_size, img_size, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", ihdr)
    png += _png_chunk(b"IDAT", zlib.compress(raw_data))
    png += _png_chunk(b"IEND", b"")

    icon_path.write_bytes(png)
    return send_from_directory(str(icon_dir), f"icon-{size}.png", mimetype="image/png")


@app.route("/api/status")
def api_status():
    """Get overall monitor status."""
    try:
        is_running = state._running
        target_uids = state.config.target_uids

        # Get last check times from DB
        conn = state._get_db()
        last_checks = {}
        for uid in target_uids:
            row = conn.execute(
                "SELECT value FROM monitor_state WHERE key = ?", (f"last_check:{uid}",)
            ).fetchone()
            if row and float(row["value"]) > 0:
                ts = float(row["value"])
                dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
                last_checks[uid] = dt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                last_checks[uid] = None

        # Counts from DB
        video_count = conn.execute("SELECT count(*) FROM seen_videos").fetchone()[0]
        dynamic_count = conn.execute("SELECT count(*) FROM seen_dynamics").fetchone()[0]
        unnotified_videos = conn.execute("SELECT count(*) FROM seen_videos WHERE notified = 0").fetchone()[0]
        unnotified_dynamics = conn.execute("SELECT count(*) FROM seen_dynamics WHERE notified = 0").fetchone()[0]

        return jsonify({
            "running": is_running,
            "target_count": len(target_uids),
            "interval_seconds": state.config.interval_seconds,
            "interval_label": _format_interval(state.config.interval_seconds),
            "last_checks": last_checks,
            "stats": {
                "total_videos": video_count,
                "total_dynamics": dynamic_count,
                "unnotified_videos": unnotified_videos,
                "unnotified_dynamics": unnotified_dynamics,
            },
            "feishu_enabled": state.config.enable_feishu,
            "local_output_enabled": state.config.enable_local_output,
            "video_download_enabled": state.config.enable_video_download,
            "cookie_status": check_cookie_status(),
            "local_ips": _get_local_ips(),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/targets")
def api_targets():
    """List all monitored targets with user info."""
    try:
        conn = state._get_db()
        targets = []
        for uid in state.config.target_uids:
            info = state.get_user_info(uid)

            # Last check time
            row = conn.execute(
                "SELECT value FROM monitor_state WHERE key = ?", (f"last_check:{uid}",)
            ).fetchone()
            last_check = None
            if row and float(row["value"]) > 0:
                ts = float(row["value"])
                dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
                last_check = dt.strftime("%Y-%m-%d %H:%M:%S")

            # Get counts for this UID
            video_count = conn.execute("SELECT count(*) FROM seen_videos WHERE uid = ?", (uid,)).fetchone()[0]
            dynamic_count = conn.execute("SELECT count(*) FROM seen_dynamics WHERE uid = ?", (uid,)).fetchone()[0]

            # Get group info for this UID
            group_row = conn.execute(
                """SELECT ng.id, ng.group_name
                   FROM notify_groups ng
                   JOIN notify_group_members ngm ON ng.id = ngm.group_id
                   WHERE ngm.uid = ?""",
                (uid,),
            ).fetchone()
            group_info = {"id": group_row["id"], "name": group_row["group_name"]} if group_row else None

            targets.append({
                "uid": uid,
                "name": info.get("name", f"UID:{uid}"),
                "face": info.get("face"),
                "space_url": info.get("space_url", f"https://space.bilibili.com/{uid}"),
                "dynamic_url": info.get("dynamic_url", f"https://space.bilibili.com/{uid}/dynamic"),
                "sign": info.get("sign", ""),
                "last_check": last_check,
                "video_count": video_count,
                "dynamic_count": dynamic_count,
                "notify_group": group_info,
            })
        return jsonify(targets)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/targets", methods=["POST"])
def api_add_target():
    """Add a new monitoring target."""
    import re as _re
    import urllib.request as _ureq
    import ssl as _ssl

    data = request.get_json(force=True)
    raw = str(data.get("uid", "")).strip()

    if not raw:
        return jsonify({"error": "UID is required"}), 400

    uid = raw

    url_match = _re.search(r'https?://[^\s<>"\']+', uid)
    if url_match:
        uid = url_match.group(0)

    b23_match = _re.search(r'b23\.tv/([A-Za-z0-9]+)', uid)
    if b23_match:
        short_url = f"https://b23.tv/{b23_match.group(1)}"
        try:
            req = _ureq.Request(short_url, headers={"User-Agent": "Mozilla/5.0"})
            resp = _ureq.urlopen(req, timeout=10)
            uid = resp.url or short_url
        except _ureq.URLError:
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            req = _ureq.Request(short_url, headers={"User-Agent": "Mozilla/5.0"})
            resp = _ureq.urlopen(req, timeout=10, context=ctx)
            uid = resp.url or short_url
        except Exception:
            uid = short_url

    match = _re.search(r"(?:space\.bilibili\.com|m\.bilibili\.com/space|bilibili\.com/space)/(\d+)", uid)
    if match:
        uid = match.group(1)

    if not uid.isdigit():
        return jsonify({"error": "Invalid UID format. Enter a UID number, space URL, or b23.tv short link"}), 400

    if uid in state.config.target_uids:
        return jsonify({"error": f"UID {uid} is already being monitored"}), 409

    state._user_cache[uid] = {
        "uid": uid,
        "name": f"UID:{uid}",
        "face": None,
        "space_url": f"https://space.bilibili.com/{uid}",
        "dynamic_url": f"https://space.bilibili.com/{uid}/dynamic",
        "sign": "",
    }

    state.config.target_uids.append(uid)

    # Persist to .env
    env = _read_env_file()
    env["BILIBILI_TARGET_UIDS"] = ",".join(state.config.target_uids)
    _write_env_file(env)

    state.add_log(f"Added UID {uid}. It will be checked on the next scheduled or manual run.")

    return jsonify({"ok": True, "uid": uid, "name": f"UID:{uid}"})


@app.route("/api/targets/<uid>", methods=["DELETE"])
def api_remove_target(uid):
    """Remove a monitoring target."""
    if uid not in state.config.target_uids:
        return jsonify({"error": f"UID {uid} is not being monitored"}), 404

    state.config.target_uids.remove(uid)

    # Persist to .env
    env = _read_env_file()
    env["BILIBILI_TARGET_UIDS"] = ",".join(state.config.target_uids)
    _write_env_file(env)

    # Clear user cache
    if uid in state._user_cache:
        del state._user_cache[uid]

    # Remove from all notify groups
    try:
        conn = state._get_db()
        conn.execute("DELETE FROM notify_group_members WHERE uid = ?", (uid,))
        conn.commit()
        conn.close()
    except Exception:
        pass

    return jsonify({"ok": True, "uid": uid})


@app.route("/api/config", methods=["GET"])
def api_get_config():
    """Get current configuration."""
    env = _read_env_file()
    return jsonify({
        "BILIBILI_TARGET_UIDS": env.get("BILIBILI_TARGET_UIDS", ""),
        "MONITOR_INTERVAL": env.get("MONITOR_INTERVAL", "5m"),
        "DB_PATH": env.get("DB_PATH", "data/monitor.db"),
        "ENABLE_FEISHU": env.get("ENABLE_FEISHU", "false"),
        "FEISHU_WEBHOOK_URL": env.get("FEISHU_WEBHOOK_URL", ""),
        "FEISHU_WEBHOOK_SECRET": env.get("FEISHU_WEBHOOK_SECRET", ""),
        "FEISHU_APP_ID": env.get("FEISHU_APP_ID", ""),
        "FEISHU_APP_SECRET": env.get("FEISHU_APP_SECRET", ""),
        "ENABLE_LOCAL_OUTPUT": env.get("ENABLE_LOCAL_OUTPUT", "true"),
        "LOCAL_OUTPUT_DIR": env.get("LOCAL_OUTPUT_DIR", "data/output"),
        "ENABLE_VIDEO_DOWNLOAD": env.get("ENABLE_VIDEO_DOWNLOAD", "false"),
        "VIDEO_DOWNLOAD_DIR": env.get("VIDEO_DOWNLOAD_DIR", "data/videos"),
        "VIDEO_DOWNLOAD_MODE": env.get("VIDEO_DOWNLOAD_MODE", "analysis-fast"),
        "TIMEZONE_OFFSET": env.get("TIMEZONE_OFFSET", "-480"),
        "RETENTION_DAYS": env.get("RETENTION_DAYS", "30"),
        "LOG_LEVEL": env.get("LOG_LEVEL", "INFO"),
    })


@app.route("/api/export-settings")
def api_export_settings():
    """Export portable settings for copying from desktop to Android."""
    try:
        env = _read_env_file()
        export_env = {key: env.get(key, "") for key in sorted(TRANSFER_ENV_KEYS) if key in env}
        conn = state._get_db()
        from store import MonitorStore
        store = MonitorStore.__new__(MonitorStore)
        store._conn = conn
        store.db_path = state.db_path
        groups = store.get_notify_groups()
        return jsonify({
            "format": "mogu-bili-settings",
            "version": 1,
            "exported_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
            "env": export_env,
            "notify_groups": groups,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/import-settings", methods=["POST"])
def api_import_settings():
    """Import portable settings exported by /api/export-settings."""
    try:
        data = request.get_json(force=True)
        env_data = data.get("env") or data.get("config") or {}
        if not isinstance(env_data, dict):
            return jsonify({"ok": False, "error": "env must be an object"}), 400

        env = _read_env_file()
        updated: dict[str, Any] = {}
        for key, value in env_data.items():
            if key in TRANSFER_ENV_KEYS:
                normalized_value = ",".join(_parse_target_uids(str(value))) if key == "BILIBILI_TARGET_UIDS" else str(value)
                env[key] = normalized_value
                updated[key] = normalized_value
        if updated:
            _write_env_file(env)
            _apply_config_to_runtime(updated)

        groups = data.get("notify_groups")
        imported_groups = 0
        imported_members = 0
        if groups is not None:
            if not isinstance(groups, list):
                return jsonify({"ok": False, "error": "notify_groups must be a list"}), 400
            conn = state._get_db()
            from store import MonitorStore
            store = MonitorStore.__new__(MonitorStore)
            store._conn = conn
            store.db_path = state.db_path
            conn.execute("DELETE FROM notify_group_members")
            conn.execute("DELETE FROM notify_groups")
            conn.commit()
            for group in groups:
                if not isinstance(group, dict):
                    continue
                name = str(group.get("group_name") or group.get("name") or "").strip()
                webhook_url = str(group.get("webhook_url") or "").strip()
                webhook_secret = str(group.get("webhook_secret") or "").strip()
                if not name or not webhook_url:
                    continue
                created = store.create_notify_group(name, webhook_url, webhook_secret)
                imported_groups += 1
                for uid_value in group.get("members", []):
                    uid = normalize_bilibili_uid(uid_value)
                    if uid and store.add_group_member(created["id"], uid):
                        imported_members += 1

        return jsonify({
            "ok": True,
            "updated": sorted(updated.keys()),
            "groups": imported_groups,
            "members": imported_members,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/config", methods=["PUT"])
def api_update_config():
    """Update configuration and persist to .env."""
    data = request.get_json(force=True)
    env = _read_env_file()

    # Whitelist of allowed keys
    allowed_keys = {
        "MONITOR_INTERVAL", "ENABLE_FEISHU", "FEISHU_WEBHOOK_URL",
        "FEISHU_WEBHOOK_SECRET", "FEISHU_APP_ID", "FEISHU_APP_SECRET",
        "ENABLE_LOCAL_OUTPUT", "LOCAL_OUTPUT_DIR",
        "ENABLE_VIDEO_DOWNLOAD", "VIDEO_DOWNLOAD_DIR", "VIDEO_DOWNLOAD_MODE",
        "TIMEZONE_OFFSET", "RETENTION_DAYS", "LOG_LEVEL",
    }

    updated = {}
    for key, value in data.items():
        if key in allowed_keys:
            env[key] = str(value)
            updated[key] = value

    if updated:
        _write_env_file(env)
        _apply_config_to_runtime(updated)

        # Reload notifier when Feishu config changes
        feishu_keys = {"ENABLE_FEISHU", "FEISHU_WEBHOOK_URL", "FEISHU_WEBHOOK_SECRET", "FEISHU_APP_ID", "FEISHU_APP_SECRET"}
        if feishu_keys & set(updated.keys()):
            if state._monitor:
                state._monitor.reload_notifier()
                logger.info("Notifier reloaded after Feishu config update")

    return jsonify({"ok": True, "updated": list(updated.keys())})


@app.route("/api/cookie", methods=["GET"])
def api_get_cookie():
    """Get current cookie status."""
    status = check_cookie_status()
    current = get_cookies()
    masked = {}
    for k, v in current.items():
        if len(v) > 8:
            masked[k] = v[:4] + "****" + v[-4:]
        else:
            masked[k] = "****"
    return jsonify({
        "status": status,
        "cookie_keys": list(current.keys()),
        "cookie_masked": masked,
        "cookie_count": len(current),
    })


@app.route("/api/cookie", methods=["POST"])
def api_set_cookie():
    """Set cookies from raw cookie string."""
    data = request.get_json(force=True)
    raw = str(data.get("cookie", "")).strip()

    if not raw:
        clear_cookies()
        env = _read_env_file()
        if "BILIBILI_COOKIE" in env:
            del env["BILIBILI_COOKIE"]
            _write_env_file(env)
        status = check_cookie_status()
        return jsonify({"ok": True, "action": "cleared", "status": status})

    cookie_dict = parse_cookie_string(raw)
    if not cookie_dict:
        return jsonify({"error": "无法解析 Cookie，请检查格式"}), 400

    set_cookies(cookie_dict)

    env = _read_env_file()
    env["BILIBILI_COOKIE"] = raw.replace("\n", "; ")
    _write_env_file(env)

    status = check_cookie_status()
    return jsonify({
        "ok": True,
        "action": "set",
        "keys": list(cookie_dict.keys()),
        "status": status,
    })


@app.route("/api/cookie", methods=["DELETE"])
def api_delete_cookie():
    """Clear all cookies."""
    clear_cookies()
    env = _read_env_file()
    if "BILIBILI_COOKIE" in env:
        del env["BILIBILI_COOKIE"]
        _write_env_file(env)
    status = check_cookie_status()
    return jsonify({"ok": True, "action": "cleared", "status": status})


@app.route("/api/cookie/verify", methods=["POST"])
def api_verify_cookie():
    """Verify current cookie validity."""
    status = check_cookie_status()
    return jsonify(status)


@app.route("/api/check", methods=["POST"])
def api_run_check():
    """Run a single check cycle."""
    try:
        monitor = BilibiliMonitor(state.config)
        results = monitor.run_once()

        # Store results
        entry = state.add_check_result(results)

        # Close the temporary monitor's store (we use our own)
        monitor.store.close()

        return jsonify({"ok": True, "result": entry})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/retry-unnotified", methods=["POST"])
def api_retry_unnotified():
    """Retry recent unnotified items without fetching Bilibili first."""
    try:
        data = request.get_json(silent=True) or {}
        lookback_hours = int(data.get("lookback_hours") or 24)
        monitor = BilibiliMonitor(state.config)
        results = monitor.retry_unnotified_all(lookback_hours=lookback_hours)
        monitor.store.close()
        retried_videos = sum(len(r.get("retried_videos", [])) for r in results)
        retried_dynamics = sum(len(r.get("retried_dynamics", [])) for r in results)
        return jsonify({
            "ok": True,
            "lookback_hours": lookback_hours,
            "retried_videos": retried_videos,
            "retried_dynamics": retried_dynamics,
            "results": results,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/start", methods=["POST"])
def api_start_monitor():
    """Start continuous monitoring in a subprocess."""
    if state._running:
        return jsonify({"error": "Monitor is already running"}), 409

    def _run_monitor():
        state._running = True
        interval = state.config.interval_seconds
        check_count = 0

        while state._running:
            check_count += 1
            now = datetime.now(timezone(timedelta(hours=8)))
            state.add_log(f"[Check #{check_count}] {now.strftime('%Y-%m-%d %H:%M:%S')}")

            try:
                monitor = BilibiliMonitor(state.config)
                results = monitor.run_once()
                state.add_check_result(results)
                monitor.store.close()

                nv = sum(len(r.get("new_videos", [])) for r in results)
                nd = sum(len(r.get("new_dynamics", [])) for r in results)
                state.add_log(f"  Check complete: {nv} new videos, {nd} new dynamics")
            except Exception as exc:
                state.add_log(f"  Check failed: {exc}")

            # Sleep in small increments
            sleep_until = time.time() + interval
            while state._running and time.time() < sleep_until:
                time.sleep(min(3, sleep_until - time.time()))

        state.add_log("Monitor stopped.")

    state._monitor_thread = threading.Thread(target=_run_monitor, daemon=True)
    state._monitor_thread.start()

    return jsonify({"ok": True, "message": "Monitor started"})


@app.route("/api/stop", methods=["POST"])
def api_stop_monitor():
    """Stop continuous monitoring."""
    if not state._running:
        return jsonify({"error": "Monitor is not running"}), 409

    state._running = False
    return jsonify({"ok": True, "message": "Monitor stopping..."})


@app.route("/api/history")
def api_history():
    """Get check history."""
    limit = request.args.get("limit", 20, type=int)
    return jsonify(state.get_history(limit))


@app.route("/api/logs")
def api_logs():
    """Get recent log entries."""
    after = request.args.get("after", 0, type=int)
    logs = state.get_logs(after)
    return jsonify({"logs": logs, "total": len(state._log_lines)})


@app.route("/api/unnotified")
def api_unnotified():
    """Get unnotified items."""
    try:
        conn = state._get_db()
        dynamics = [dict(r) for r in conn.execute(
            "SELECT * FROM seen_dynamics WHERE notified = 0 ORDER BY first_seen_ts DESC LIMIT 50"
        ).fetchall()]
        videos = [dict(r) for r in conn.execute(
            "SELECT * FROM seen_videos WHERE notified = 0 ORDER BY first_seen_ts DESC LIMIT 50"
        ).fetchall()]
        return jsonify({
            "dynamics": dynamics,
            "videos": videos,
            "dynamics_count": len(dynamics),
            "videos_count": len(videos),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/seen")
def api_seen():
    """Get recently seen videos and dynamics."""
    try:
        limit = request.args.get("limit", 30, type=int)
        conn = state._get_db()
        videos = [dict(r) for r in conn.execute(
            "SELECT * FROM seen_videos ORDER BY first_seen_ts DESC LIMIT ?", (limit,)
        ).fetchall()]
        dynamics = [dict(r) for r in conn.execute(
            "SELECT * FROM seen_dynamics ORDER BY first_seen_ts DESC LIMIT ?", (limit,)
        ).fetchall()]
        return jsonify({"videos": videos, "dynamics": dynamics})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/cleanup", methods=["POST"])
def api_cleanup():
    """Run cleanup of old records."""
    try:
        cutoff = time.time() - state.config.retention_days * 86400
        conn = state._get_db()
        dyn_result = conn.execute("DELETE FROM seen_dynamics WHERE first_seen_ts < ?", (cutoff,))
        vid_result = conn.execute("DELETE FROM seen_videos WHERE first_seen_ts < ?", (cutoff,))
        conn.commit()
        return jsonify({
            "ok": True,
            "dynamics_deleted": dyn_result.rowcount,
            "videos_deleted": vid_result.rowcount,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/refresh-user/<uid>", methods=["POST"])
def api_refresh_user(uid):
    """Force refresh user info cache for a UID."""
    if uid in state._user_cache:
        del state._user_cache[uid]
    info = state.get_user_info(uid)
    return jsonify({"ok": True, "info": info})


# ---- Notify Groups API ----

@app.route("/api/notify-groups")
def api_get_notify_groups():
    """List all notify groups."""
    try:
        conn = state._get_db()
        from store import MonitorStore
        store = MonitorStore.__new__(MonitorStore)
        store._conn = conn
        store.db_path = state.db_path
        groups = store.get_notify_groups()
        for g in groups:
            g["member_info"] = []
        for uid in g.get("members", []):
            normalized_uid = normalize_bilibili_uid(uid)
            info = state.get_user_info(normalized_uid)
            g["member_info"].append({"uid": normalized_uid, "name": info.get("name", f"UID:{normalized_uid}")})
        return jsonify(groups)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/notify-groups", methods=["POST"])
def api_create_notify_group():
    """Create a new notify group."""
    data = request.get_json(force=True)
    group_name = str(data.get("group_name", "")).strip()
    webhook_url = str(data.get("webhook_url", "")).strip()
    webhook_secret = str(data.get("webhook_secret", "")).strip()

    if not group_name:
        return jsonify({"error": "分组名称不能为空"}), 400
    if not webhook_url:
        return jsonify({"error": "Webhook URL 不能为空"}), 400

    try:
        conn = state._get_db()
        from store import MonitorStore
        store = MonitorStore.__new__(MonitorStore)
        store._conn = conn
        store.db_path = state.db_path
        group = store.create_notify_group(group_name, webhook_url, webhook_secret)
        return jsonify({"ok": True, "group": group}), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/notify-groups/<int:group_id>", methods=["PUT"])
def api_update_notify_group(group_id):
    """Update a notify group."""
    data = request.get_json(force=True)
    group_name = data.get("group_name")
    webhook_url = data.get("webhook_url")
    webhook_secret = data.get("webhook_secret")

    try:
        conn = state._get_db()
        from store import MonitorStore
        store = MonitorStore.__new__(MonitorStore)
        store._conn = conn
        store.db_path = state.db_path
        ok = store.update_notify_group(group_id, group_name, webhook_url, webhook_secret)
        if not ok:
            return jsonify({"error": "分组不存在或无更新"}), 404
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/notify-groups/<int:group_id>", methods=["DELETE"])
def api_delete_notify_group(group_id):
    """Delete a notify group and its members."""
    try:
        conn = state._get_db()
        from store import MonitorStore
        store = MonitorStore.__new__(MonitorStore)
        store._conn = conn
        store.db_path = state.db_path
        ok = store.delete_notify_group(group_id)
        if not ok:
            return jsonify({"error": "分组不存在"}), 404
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/notify-groups/<int:group_id>/members", methods=["POST"])
def api_add_group_member(group_id):
    """Add a UID to a notify group."""
    data = request.get_json(force=True)
    uid = normalize_bilibili_uid(data.get("uid", ""))

    if not uid:
        return jsonify({"error": "UID 不能为空"}), 400

    try:
        conn = state._get_db()
        from store import MonitorStore
        store = MonitorStore.__new__(MonitorStore)
        store._conn = conn
        store.db_path = state.db_path

        group = store.get_notify_group(group_id)
        if not group:
            return jsonify({"error": "分组不存在"}), 404

        ok = store.add_group_member(group_id, uid)
        return jsonify({"ok": ok})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/notify-groups/<int:group_id>/members/<uid>", methods=["DELETE"])
def api_remove_group_member(group_id, uid):
    """Remove a UID from a notify group."""
    try:
        uid = normalize_bilibili_uid(uid)
        conn = state._get_db()
        from store import MonitorStore
        store = MonitorStore.__new__(MonitorStore)
        store._conn = conn
        store.db_path = state.db_path
        ok = store.remove_group_member(group_id, uid)
        if not ok:
            return jsonify({"error": "成员不在该分组中"}), 404
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/test-feishu", methods=["POST"])
def api_test_feishu():
    """Send a test message to a Feishu webhook to verify connectivity."""
    data = request.get_json(force=True)
    webhook_url = str(data.get("webhook_url", "")).strip()
    webhook_secret = str(data.get("webhook_secret", "")).strip()
    group_id = data.get("group_id")
    image_url = str(data.get("image_url", "")).strip()

    if not webhook_url:
        if group_id:
            try:
                conn = state._get_db()
                from store import MonitorStore
                store = MonitorStore.__new__(MonitorStore)
                store._conn = conn
                store.db_path = state.db_path
                group = store.get_notify_group(int(group_id))
                if group:
                    webhook_url = group["webhook_url"]
                    webhook_secret = group.get("webhook_secret", "")
            except Exception:
                pass
        if not webhook_url:
            webhook_url = state.config.feishu_webhook_url
            webhook_secret = state.config.feishu_webhook_secret

    if not webhook_url:
        return jsonify({"ok": False, "error": "未配置 Webhook URL"}), 400

    try:
        from notifier import FeishuNotifier
        feishu = FeishuNotifier(webhook_url, webhook_secret, app_id=state.config.feishu_app_id, app_secret=state.config.feishu_app_secret)
        image_url_used = image_url or _find_recent_image_url()
        result = feishu.send_interactive_card(
            title="Bilibili Monitor - Test",
            content_lines=[
                "**This is a test message**",
                "If you see this, the Feishu bot is configured correctly!",
                f"Time: {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')}",
                "This test also verifies Feishu image upload when a recent image is available.",
            ],
            header_color="turquoise",
            image_urls=[image_url_used] if image_url_used else [],
        )
        image_errors = getattr(feishu, "last_image_upload_errors", [])
        if result.get("code") == 0:
            return jsonify({
                "ok": True,
                "msg": "Test message sent successfully",
                "image_url_used": image_url_used,
                "image_upload_errors": image_errors,
                "image_uploaded": bool(image_url_used and not image_errors),
                "has_app_auth": bool(state.config.feishu_app_id and state.config.feishu_app_secret),
            })
        else:
            return jsonify({
                "ok": False,
                "error": f"Feishu returned error: {result.get('msg', 'unknown')}",
                "detail": result,
                "image_url_used": image_url_used,
                "image_upload_errors": image_errors,
                "has_app_auth": bool(state.config.feishu_app_id and state.config.feishu_app_secret),
            })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ---- Helpers ----

def _format_interval(seconds: int) -> str:
    """Format seconds to human-readable interval."""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    else:
        return f"{seconds // 3600}h"


def create_app(config: MonitorConfig):
    """Create and configure the Flask app with the given config."""
    global state
    state = MonitorState(config)

    # Load saved cookies from .env
    env = _read_env_file()
    saved_cookie = env.get("BILIBILI_COOKIE", "")
    if saved_cookie:
        cookie_dict = parse_cookie_string(saved_cookie)
        if cookie_dict:
            set_cookies(cookie_dict)
            logger.info(f"Loaded {len(cookie_dict)} cookie keys from .env")

    return app


# ---- Main ----

def main():
    parser = argparse.ArgumentParser(description="Bilibili UP Monitor Dashboard")
    parser.add_argument("--port", type=int, default=8199, help="Web server port (default: 8199)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    parser.add_argument("--env", default=".env", help="Path to .env config file")
    args = parser.parse_args()

    project_root = _EXE_DIR
    env_file = project_root / args.env

    # Auto-create .env if it doesn't exist
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
        print(f"Created default config: {env_file}")

    # Load config
    config = load_config(str(env_file))
    config.base_dir = project_root

    # Setup logging
    setup_logging(config)

    # Initialize app and global state
    create_app(config)

    # Warm up user cache
    for uid in config.target_uids:
        state.get_user_info(uid)

    print(f"""
==================================================
  Bilibili UP Monitor Dashboard
==================================================
  URL:  http://localhost:{args.port}
  UIDs: {len(config.target_uids)} targets configured
  Interval: {_format_interval(config.interval_seconds)}
==================================================
""")

    # Auto-open browser after a short delay
    import webbrowser
    url = f"http://localhost:{args.port}"
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()

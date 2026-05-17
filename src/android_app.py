#!/usr/bin/env python3
"""Bili Monitor - Server module for Android.

This module provides ONLY server functions:
  init_app()      - Initialize paths, logging, env
  start_server()  - Build and run Flask server (blocking, run in thread)
  check_server()  - Non-blocking check if server is up
  get_error()     - Read server error file
  write_log(name, text) - Write a log file

NO Kivy code here. NO module-level side effects beyond safe constants.
"""

import os
import sys
from pathlib import Path

SRC = Path(__file__).parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PORT = 8199
IS_ANDROID = hasattr(sys, "_android_api")

_base_dir = None
_data_dir = None

TRANSFER_ENV_KEYS = {
    "BILIBILI_TARGET_UIDS", "MONITOR_INTERVAL", "ENABLE_FEISHU",
    "FEISHU_WEBHOOK_URL", "FEISHU_WEBHOOK_SECRET", "FEISHU_APP_ID", "FEISHU_APP_SECRET",
    "BILIBILI_COOKIE",
    "ENABLE_LOCAL_OUTPUT", "ENABLE_VIDEO_DOWNLOAD", "VIDEO_DOWNLOAD_MODE",
    "TIMEZONE_OFFSET", "RETENTION_DAYS", "LOG_LEVEL",
}


def write_log(name, text):
    """Write a log file to the data directory."""
    if _data_dir:
        try:
            (_data_dir / name).write_text(str(text), encoding="utf-8")
        except Exception:
            pass


def write_file(name, text):
    """Write a file to the base directory (for config files like .env)."""
    if _base_dir:
        try:
            (_base_dir / name).write_text(str(text), encoding="utf-8")
        except Exception:
            pass


def get_error():
    if not _data_dir:
        return None
    p = _data_dir / "server_error.txt"
    if p.exists():
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            pass
    return None


def _get_app_dir():
    if not IS_ANDROID:
        return SRC.parent
    try:
        from android.storage import app_storage_path
        p = Path(app_storage_path())
        if p.exists():
            return p
    except Exception:
        pass
    try:
        from jnius import autoclass
        pa = autoclass("org.kivy.android.PythonActivity")
        p = Path(pa.mActivity.getFilesDir().getAbsolutePath())
        if p.exists():
            return p
    except Exception:
        pass
    try:
        from jnius import autoclass
        pa = autoclass("org.kivy.android.PythonActivity")
        ext = pa.mActivity.getExternalFilesDir(None)
        if ext:
            p = Path(ext.getAbsolutePath())
            if p.exists():
                return p
    except Exception:
        pass
    return Path("/data/data/org.bilibilimonitor/files")


def init_app():
    global _base_dir, _data_dir
    _base_dir = Path(_get_app_dir())
    _data_dir = _base_dir / "data"
    for d in [_data_dir, _data_dir / "output", _data_dir / "videos", _base_dir / "logs"]:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    import logging
    try:
        fh = logging.FileHandler(str(_data_dir / "app.log"), encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(fh)
        logging.getLogger().setLevel(logging.DEBUG)
    except Exception:
        pass

    env_file = _base_dir / ".env"
    if not env_file.exists():
        write_file(
            ".env",
            "BILIBILI_TARGET_UIDS=\n"
            "MONITOR_INTERVAL=5m\n"
            "DB_PATH=data/monitor.db\n"
            "ENABLE_FEISHU=false\n"
            "FEISHU_WEBHOOK_URL=\n"
            "FEISHU_WEBHOOK_SECRET=\n"
            "FEISHU_APP_ID=\n"
            "FEISHU_APP_SECRET=\n"
            "ENABLE_LOCAL_OUTPUT=true\n"
            "LOCAL_OUTPUT_DIR=data/output\n"
            "ENABLE_VIDEO_DOWNLOAD=false\n"
            "VIDEO_DOWNLOAD_DIR=data/videos\n"
            "VIDEO_DOWNLOAD_MODE=analysis-fast\n"
            "TIMEZONE_OFFSET=-480\n"
            "RETENTION_DAYS=30\n"
            "LOG_LEVEL=INFO\n",
        )

    write_log("startup.txt", f"IS_ANDROID={IS_ANDROID}\nbase_dir={_base_dir}\ndata_dir={_data_dir}\nPORT={PORT}")


def check_server():
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2)
        return True
    except Exception:
        return False


def start_server():
    import time
    import traceback

    log = __import__("logging").getLogger("bili.server")
    try:
        log.info("Building Flask app...")
        flask_app = _build_flask_app()
        log.info("Flask app built, starting on 0.0.0.0:%d", PORT)
        write_log("server_status.txt", f"starting at {time.strftime('%H:%M:%S')}")
        flask_app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True, use_reloader=False)
    except Exception as e:
        log.error("Server FAILED: %s", e)
        log.error(traceback.format_exc())
        write_log("server_error.txt", f"{time.strftime('%H:%M:%S')}\n{e}\n\n{traceback.format_exc()}")


def _build_flask_app():
    import sqlite3
    import time
    import re
    import json
    from datetime import datetime, timezone, timedelta
    from flask import Flask, jsonify, request, send_from_directory

    from config import load_config, _parse_interval
    from store import CREATE_TABLES_SQL, normalize_bilibili_uid
    from fetcher import (
        fetch_user_info, set_cookies, get_cookies, clear_cookies,
        parse_cookie_string, check_cookie_status,
    )
    from monitor import BilibiliMonitor

    os.environ["DB_PATH"] = str(_data_dir / "monitor.db")
    os.environ["LOCAL_OUTPUT_DIR"] = str(_data_dir / "output")
    os.environ["VIDEO_DOWNLOAD_DIR"] = str(_data_dir / "videos")

    config = load_config(str(_base_dir / ".env"))
    config.base_dir = _base_dir
    saved_env = {}
    env_path = _base_dir / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                saved_env[k.strip()] = v.strip()
    saved_cookie = saved_env.get("BILIBILI_COOKIE", "")
    if saved_cookie:
        cookie_dict = parse_cookie_string(saved_cookie)
        if cookie_dict:
            set_cookies(cookie_dict)

    flask_app = Flask(__name__, static_folder=None)
    db_path = str(_data_dir / "monitor.db")
    user_cache = {}

    def _get_db():
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(CREATE_TABLES_SQL)
        return conn

    def _read_env_dict():
        result = {}
        env = _base_dir / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    result[k.strip()] = v.strip()
        return result

    def _write_env_dict(values):
        lines_out = []
        written = set()
        env = _base_dir / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    lines_out.append(line)
                    continue
                if "=" in stripped:
                    k, _, _ = stripped.partition("=")
                    k = k.strip()
                    if k in values:
                        lines_out.append(f"{k}={values[k]}")
                        written.add(k)
                    else:
                        lines_out.append(line)
                else:
                    lines_out.append(line)
        for k, v in values.items():
            if k not in written:
                lines_out.append(f"{k}={v}")
        write_file(".env", "\n".join(lines_out) + "\n")

    def _parse_target_uids(raw):
        uids = []
        for token in str(raw or "").split(","):
            uid = normalize_bilibili_uid(token)
            if uid and uid.isdigit() and uid not in uids:
                uids.append(uid)
        return uids

    def _apply_config_to_runtime(updated):
        if "BILIBILI_TARGET_UIDS" in updated:
            config.target_uids = _parse_target_uids(updated["BILIBILI_TARGET_UIDS"])
            user_cache.clear()
        if "MONITOR_INTERVAL" in updated:
            try:
                config.interval_seconds = _parse_interval(updated["MONITOR_INTERVAL"])
            except ValueError:
                pass
        if "ENABLE_FEISHU" in updated:
            config.enable_feishu = str(updated["ENABLE_FEISHU"]).lower() in ("true", "1", "yes")
        if "FEISHU_WEBHOOK_URL" in updated:
            config.feishu_webhook_url = str(updated["FEISHU_WEBHOOK_URL"])
        if "FEISHU_WEBHOOK_SECRET" in updated:
            config.feishu_webhook_secret = str(updated["FEISHU_WEBHOOK_SECRET"])
        if "FEISHU_APP_ID" in updated:
            config.feishu_app_id = str(updated["FEISHU_APP_ID"])
        if "FEISHU_APP_SECRET" in updated:
            config.feishu_app_secret = str(updated["FEISHU_APP_SECRET"])
        if "BILIBILI_COOKIE" in updated:
            cookie_dict = parse_cookie_string(str(updated["BILIBILI_COOKIE"]))
            if cookie_dict:
                set_cookies(cookie_dict)
            else:
                clear_cookies()
        if "ENABLE_LOCAL_OUTPUT" in updated:
            config.enable_local_output = str(updated["ENABLE_LOCAL_OUTPUT"]).lower() in ("true", "1", "yes")
        if "ENABLE_VIDEO_DOWNLOAD" in updated:
            config.enable_video_download = str(updated["ENABLE_VIDEO_DOWNLOAD"]).lower() in ("true", "1", "yes")
        if "VIDEO_DOWNLOAD_MODE" in updated:
            config.video_download_mode = str(updated["VIDEO_DOWNLOAD_MODE"])
        if "RETENTION_DAYS" in updated:
            try:
                config.retention_days = int(updated["RETENTION_DAYS"])
            except ValueError:
                pass
        if "LOG_LEVEL" in updated:
            config.log_level = str(updated["LOG_LEVEL"])

    def _reload_runtime_config_from_env():
        _apply_config_to_runtime(_read_env_dict())

    def _feishu_diagnostics():
        conn = _get_db()
        try:
            groups = conn.execute("SELECT id, group_name, webhook_url FROM notify_groups ORDER BY id").fetchall()
            members = conn.execute("SELECT group_id, uid FROM notify_group_members ORDER BY group_id, uid").fetchall()
            group_list = []
            for group in groups:
                group_members = [m["uid"] for m in members if m["group_id"] == group["id"]]
                group_list.append({
                    "id": group["id"],
                    "name": group["group_name"],
                    "has_webhook": bool(group["webhook_url"]),
                    "members": group_members,
                })
        finally:
            conn.close()
        return {
            "enable_feishu": config.enable_feishu,
            "has_default_webhook": bool(config.feishu_webhook_url),
            "target_uids": list(config.target_uids),
            "groups": group_list,
        }

    def _find_recent_image_url():
        conn = _get_db()
        try:
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
        finally:
            conn.close()
        return ""

    def _get_user(uid):
        if uid not in user_cache:
            try:
                user_cache[uid] = fetch_user_info(uid)
            except Exception:
                user_cache[uid] = {
                    "uid": uid,
                    "name": f"UID:{uid}",
                    "face": None,
                    "space_url": f"https://space.bilibili.com/{uid}",
                    "dynamic_url": f"https://space.bilibili.com/{uid}/dynamic",
                    "sign": "",
                }
        return user_cache[uid]

    monitor_running = False
    check_history = []
    log_lines = []
    _lock = __import__("threading").Lock()

    def _add_log(line):
        with _lock:
            log_lines.append(line)
            if len(log_lines) > 500:
                del log_lines[:100]

    def _add_check(results):
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
        with _lock:
            check_history.insert(0, entry)
            if len(check_history) > 100:
                del check_history[100:]
        try:
            conn = _get_db()
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
            conn.close()
        except Exception:
            pass
        return entry

    def _read_env_values():
        env = _base_dir / ".env"
        result = {}
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    result[k.strip()] = v.strip()
        return result

    def _write_env_values(values):
        env = _base_dir / ".env"
        lines = []
        written = set()
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    lines.append(line)
                    continue
                k, _, _ = stripped.partition("=")
                k = k.strip()
                if k in values:
                    lines.append(f"{k}={values[k]}")
                    written.add(k)
                else:
                    lines.append(line)
        for k, v in values.items():
            if k not in written:
                lines.append(f"{k}={v}")
        write_file(".env", "\n".join(lines) + "\n")

    @flask_app.route("/")
    def index():
        return send_from_directory(str(SRC), "dashboard.html")

    @flask_app.route("/icon.png")
    def icon_png():
        return send_from_directory(str(SRC), "icon.png", mimetype="image/png")

    @flask_app.route("/manifest.json")
    def manifest():
        return send_from_directory(str(SRC), "manifest.json", mimetype="application/json")

    @flask_app.route("/api/status")
    def api_status():
        try:
            _reload_runtime_config_from_env()
            conn = _get_db()
            last_checks = {}
            for uid in config.target_uids:
                row = conn.execute(
                    "SELECT value FROM monitor_state WHERE key = ?",
                    (f"last_check:{uid}",),
                ).fetchone()
                if row and float(row["value"]) > 0:
                    ts = float(row["value"])
                    dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
                    last_checks[uid] = dt.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    last_checks[uid] = None
            vc = conn.execute("SELECT count(*) FROM seen_videos").fetchone()[0]
            dc = conn.execute("SELECT count(*) FROM seen_dynamics").fetchone()[0]
            uv = conn.execute("SELECT count(*) FROM seen_videos WHERE notified = 0").fetchone()[0]
            ud = conn.execute("SELECT count(*) FROM seen_dynamics WHERE notified = 0").fetchone()[0]
            conn.close()
            return jsonify({
                "running": monitor_running,
                "target_count": len(config.target_uids),
                "interval_seconds": config.interval_seconds,
                "interval_label": f"{config.interval_seconds // 60}m",
                "last_checks": last_checks,
                "stats": {"total_videos": vc, "total_dynamics": dc, "unnotified_videos": uv, "unnotified_dynamics": ud},
                "feishu_enabled": config.enable_feishu,
                "feishu_diagnostics": _feishu_diagnostics(),
                "local_output_enabled": config.enable_local_output,
                "video_download_enabled": config.enable_video_download,
                "cookie_status": check_cookie_status(),
                "local_ips": [],
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @flask_app.route("/api/targets")
    def api_targets():
        try:
            conn = _get_db()
            targets = []
            for uid in config.target_uids:
                info = _get_user(uid)
                row = conn.execute("SELECT value FROM monitor_state WHERE key = ?", (f"last_check:{uid}",)).fetchone()
                last_check = None
                if row and float(row["value"]) > 0:
                    ts = float(row["value"])
                    dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
                    last_check = dt.strftime("%Y-%m-%d %H:%M:%S")
                vc = conn.execute("SELECT count(*) FROM seen_videos WHERE uid = ?", (uid,)).fetchone()[0]
                dc = conn.execute("SELECT count(*) FROM seen_dynamics WHERE uid = ?", (uid,)).fetchone()[0]
                group_row = conn.execute(
                    "SELECT ng.id, ng.group_name FROM notify_groups ng "
                    "JOIN notify_group_members ngm ON ng.id = ngm.group_id WHERE ngm.uid = ?", (uid,)
                ).fetchone()
                group_info = {"id": group_row["id"], "name": group_row["group_name"]} if group_row else None
                targets.append({
                    "uid": uid, "name": info.get("name", f"UID:{uid}"), "face": info.get("face"),
                    "space_url": info.get("space_url", f"https://space.bilibili.com/{uid}"),
                    "dynamic_url": info.get("dynamic_url", f"https://space.bilibili.com/{uid}/dynamic"),
                    "sign": info.get("sign", ""), "last_check": last_check,
                    "video_count": vc, "dynamic_count": dc, "notify_group": group_info,
                })
            conn.close()
            return jsonify(targets)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @flask_app.route("/api/targets", methods=["POST"])
    def api_add_target():
        import re as _re
        import urllib.request as _ureq
        import ssl as _ssl

        data = request.get_json(force=True)
        raw = str(data.get("uid", "")).strip()
        if not raw:
            return jsonify({"error": "UID required"}), 400

        uid = raw

        url_match = _re.search(r'https?://[^\s<>"\']+', uid)
        if url_match:
            uid = url_match.group(0)

        b23_match = _re.search(r'b23\.tv/([A-Za-z0-9]+)', uid)
        if b23_match:
            short_url = f"https://b23.tv/{b23_match.group(1)}"
            try:
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
            return jsonify({"error": "无效的UID或链接格式，请输入UID数字、空间链接或b23.tv短链接"}), 400
        if uid in config.target_uids:
            return jsonify({"error": "Already monitored"}), 409
        user_cache[uid] = {
            "uid": uid,
            "name": f"UID:{uid}",
            "face": None,
            "space_url": f"https://space.bilibili.com/{uid}",
            "dynamic_url": f"https://space.bilibili.com/{uid}/dynamic",
            "sign": "",
        }
        config.target_uids.append(uid)
        env = _base_dir / ".env"
        if env.exists():
            lines = env.read_text(encoding="utf-8").splitlines()
            out = []
            found = False
            for line in lines:
                if line.startswith("BILIBILI_TARGET_UIDS="):
                    out.append(f"BILIBILI_TARGET_UIDS={','.join(config.target_uids)}")
                    found = True
                else:
                    out.append(line)
            if not found:
                out.append(f"BILIBILI_TARGET_UIDS={','.join(config.target_uids)}")
            write_file(".env", "\n".join(out) + "\n")
        _add_log(f"Added UID {uid}. It will be checked on the next scheduled or manual run.")
        return jsonify({"ok": True, "uid": uid, "name": f"UID:{uid}"})

    @flask_app.route("/api/targets/<uid>", methods=["DELETE"])
    def api_remove_target(uid):
        if uid not in config.target_uids:
            return jsonify({"error": "Not monitored"}), 404
        config.target_uids.remove(uid)
        user_cache.pop(uid, None)
        env = _base_dir / ".env"
        if env.exists():
            lines = env.read_text(encoding="utf-8").splitlines()
            out = []
            found = False
            for line in lines:
                if line.startswith("BILIBILI_TARGET_UIDS="):
                    out.append(f"BILIBILI_TARGET_UIDS={','.join(config.target_uids)}")
                    found = True
                else:
                    out.append(line)
            if not found:
                out.append(f"BILIBILI_TARGET_UIDS={','.join(config.target_uids)}")
            write_file(".env", "\n".join(out) + "\n")
        try:
            conn = _get_db()
            conn.execute("DELETE FROM notify_group_members WHERE uid = ?", (uid,))
            conn.commit()
            conn.close()
        except Exception:
            pass
        return jsonify({"ok": True, "uid": uid})

    @flask_app.route("/api/config", methods=["GET"])
    def api_get_config():
        result = _read_env_dict()
        defaults = {
            "BILIBILI_TARGET_UIDS": "", "MONITOR_INTERVAL": "5m", "DB_PATH": "data/monitor.db",
            "ENABLE_FEISHU": "false", "FEISHU_WEBHOOK_URL": "", "FEISHU_WEBHOOK_SECRET": "",
            "FEISHU_APP_ID": "", "FEISHU_APP_SECRET": "",
            "ENABLE_LOCAL_OUTPUT": "true", "LOCAL_OUTPUT_DIR": "data/output",
            "ENABLE_VIDEO_DOWNLOAD": "false", "VIDEO_DOWNLOAD_DIR": "data/videos",
            "VIDEO_DOWNLOAD_MODE": "analysis-fast", "TIMEZONE_OFFSET": "-480",
            "RETENTION_DAYS": "30", "LOG_LEVEL": "INFO",
        }
        for k, v in defaults.items():
            result.setdefault(k, v)
        return jsonify(result)

    @flask_app.route("/api/export-settings")
    def api_export_settings():
        try:
            env_values = _read_env_dict()
            export_env = {key: env_values.get(key, "") for key in sorted(TRANSFER_ENV_KEYS) if key in env_values}
            conn = _get_db()
            groups = []
            rows = conn.execute("SELECT id, group_name, webhook_url, webhook_secret, created_ts FROM notify_groups ORDER BY id").fetchall()
            for row in rows:
                group = dict(row)
                members = conn.execute("SELECT uid FROM notify_group_members WHERE group_id = ?", (group["id"],)).fetchall()
                group["members"] = [normalize_bilibili_uid(m["uid"]) for m in members if normalize_bilibili_uid(m["uid"])]
                groups.append(group)
            conn.close()
            return jsonify({
                "format": "mogu-bili-settings",
                "version": 1,
                "exported_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
                "env": export_env,
                "notify_groups": groups,
            })
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @flask_app.route("/api/import-settings", methods=["POST"])
    def api_import_settings():
        try:
            data = request.get_json(force=True)
            env_data = data.get("env") or data.get("config") or {}
            if not isinstance(env_data, dict):
                return jsonify({"ok": False, "error": "env must be an object"}), 400
            current = _read_env_dict()
            updated = {}
            for k, v in env_data.items():
                if k in TRANSFER_ENV_KEYS:
                    normalized_value = ",".join(_parse_target_uids(str(v))) if k == "BILIBILI_TARGET_UIDS" else str(v)
                    current[k] = normalized_value
                    updated[k] = normalized_value
            if updated:
                _write_env_dict(current)
                _apply_config_to_runtime(updated)

            imported_groups = 0
            imported_members = 0
            groups = data.get("notify_groups")
            if groups is not None:
                if not isinstance(groups, list):
                    return jsonify({"ok": False, "error": "notify_groups must be a list"}), 400
                conn = _get_db()
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
                    cur = conn.execute(
                        "INSERT INTO notify_groups (group_name, webhook_url, webhook_secret, created_ts) VALUES (?, ?, ?, ?)",
                        (name, webhook_url, webhook_secret, time.time()),
                    )
                    imported_groups += 1
                    gid = cur.lastrowid
                    for uid_value in group.get("members", []):
                        uid = normalize_bilibili_uid(uid_value)
                        if not uid:
                            continue
                        conn.execute("INSERT OR IGNORE INTO notify_group_members (group_id, uid) VALUES (?, ?)", (gid, uid))
                        imported_members += 1
                conn.commit()
                conn.close()
            return jsonify({"ok": True, "updated": sorted(updated.keys()), "groups": imported_groups, "members": imported_members})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @flask_app.route("/api/config", methods=["PUT"])
    def api_update_config():
        data = request.get_json(force=True)
        current = _read_env_dict()
        allowed = {"MONITOR_INTERVAL", "ENABLE_FEISHU", "FEISHU_WEBHOOK_URL", "FEISHU_WEBHOOK_SECRET",
                    "FEISHU_APP_ID", "FEISHU_APP_SECRET",
                    "ENABLE_LOCAL_OUTPUT", "LOCAL_OUTPUT_DIR", "ENABLE_VIDEO_DOWNLOAD",
                    "VIDEO_DOWNLOAD_DIR", "VIDEO_DOWNLOAD_MODE", "TIMEZONE_OFFSET", "RETENTION_DAYS", "LOG_LEVEL"}
        updated = {}
        for k, v in data.items():
            if k in allowed:
                current[k] = str(v)
                updated[k] = str(v)
        _write_env_dict(current)
        _apply_config_to_runtime(updated)
        return jsonify({"ok": True})

    @flask_app.route("/api/cookie", methods=["GET"])
    def api_get_cookie():
        status = check_cookie_status()
        current = get_cookies()
        masked = {}
        for k, v in current.items():
            masked[k] = v[:4] + "****" + v[-4:] if len(v) > 8 else "****"
        return jsonify({
            "status": status,
            "cookie_keys": list(current.keys()),
            "cookie_masked": masked,
            "cookie_count": len(current),
        })

    @flask_app.route("/api/cookie", methods=["POST"])
    def api_set_cookie():
        data = request.get_json(force=True)
        raw = str(data.get("cookie", "")).strip()
        if not raw:
            clear_cookies()
            values = _read_env_values()
            values.pop("BILIBILI_COOKIE", None)
            _write_env_values(values)
            return jsonify({"ok": True, "action": "cleared", "status": check_cookie_status()})
        cookie_dict = parse_cookie_string(raw)
        if not cookie_dict:
            return jsonify({"error": "无法解析 Cookie，请检查格式"}), 400
        set_cookies(cookie_dict)
        values = _read_env_values()
        values["BILIBILI_COOKIE"] = raw.replace("\n", "; ")
        _write_env_values(values)
        return jsonify({"ok": True, "action": "set", "keys": list(cookie_dict.keys()), "status": check_cookie_status()})

    @flask_app.route("/api/cookie", methods=["DELETE"])
    def api_delete_cookie():
        clear_cookies()
        values = _read_env_values()
        values.pop("BILIBILI_COOKIE", None)
        _write_env_values(values)
        return jsonify({"ok": True, "action": "cleared", "status": check_cookie_status()})

    @flask_app.route("/api/cookie/verify", methods=["POST"])
    def api_verify_cookie():
        return jsonify(check_cookie_status())

    @flask_app.route("/api/check", methods=["POST"])
    def api_run_check():
        try:
            _reload_runtime_config_from_env()
            mon = BilibiliMonitor(config)
            results = mon.run_once()
            delivery_errors = getattr(mon.notifier, "last_errors", [])
            entry = _add_check(results)
            mon.store.close()
            return jsonify({"ok": True, "result": entry, "delivery_errors": delivery_errors})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @flask_app.route("/api/retry-unnotified", methods=["POST"])
    def api_retry_unnotified():
        try:
            _reload_runtime_config_from_env()
            data = request.get_json(silent=True) or {}
            lookback_hours = int(data.get("lookback_hours") or 72)
            mon = BilibiliMonitor(config)
            results = mon.retry_unnotified_all(lookback_hours=lookback_hours)
            delivery_errors = getattr(mon.notifier, "last_errors", [])
            mon.store.close()
            return jsonify({
                "ok": True,
                "lookback_hours": lookback_hours,
                "retried_videos": sum(len(r.get("retried_videos", [])) for r in results),
                "retried_dynamics": sum(len(r.get("retried_dynamics", [])) for r in results),
                "results": results,
                "delivery_errors": delivery_errors,
                "feishu_diagnostics": _feishu_diagnostics(),
            })
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @flask_app.route("/api/start", methods=["POST"])
    def api_start():
        nonlocal monitor_running
        if monitor_running:
            return jsonify({"error": "Already running"}), 409

        def _run():
            nonlocal monitor_running
            monitor_running = True
            interval = config.interval_seconds
            n = 0
            _add_log(f"Monitor started, interval={interval}s, targets={len(config.target_uids)}")
            while monitor_running:
                n += 1
                now = datetime.now(timezone(timedelta(hours=8)))
                _add_log(f"[#{n}] {now.strftime('%H:%M:%S')} checking {len(config.target_uids)} targets...")
                try:
                    _reload_runtime_config_from_env()
                    mon = BilibiliMonitor(config)
                    results = mon.run_once()
                    delivery_errors = getattr(mon.notifier, "last_errors", [])
                    _add_check(results)
                    mon.store.close()
                    total_v = sum(len(r.get("new_videos", [])) for r in results)
                    total_d = sum(len(r.get("new_dynamics", [])) for r in results)
                    total_e = sum(1 for r in results if r.get("error"))
                    _add_log(f"  Done: {total_v} new videos, {total_d} new dynamics, {total_e} errors")
                    for err in delivery_errors[:5]:
                        _add_log(f"  Feishu: {err}")
                except Exception as e:
                    _add_log(f"  Fail: {e}")
                end = time.time() + interval
                while monitor_running and time.time() < end:
                    time.sleep(min(3, end - time.time()))
            _add_log("Monitor stopped.")

        import threading
        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True})

    @flask_app.route("/api/stop", methods=["POST"])
    def api_stop():
        nonlocal monitor_running
        if not monitor_running:
            return jsonify({"error": "Not running"}), 409
        monitor_running = False
        return jsonify({"ok": True})

    @flask_app.route("/api/logs")
    def api_logs():
        after = request.args.get("after", 0, type=int)
        with _lock:
            return jsonify({"logs": log_lines[after:], "total": len(log_lines)})

    @flask_app.route("/api/history")
    def api_history():
        limit = request.args.get("limit", 20, type=int)
        with _lock:
            if check_history:
                return jsonify(check_history[:limit])
        try:
            conn = _get_db()
            rows = conn.execute(
                "SELECT timestamp, ts, new_videos, new_dynamics, errors, summary FROM check_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
            if rows:
                return jsonify([dict(r) for r in rows])
        except Exception:
            pass
        return jsonify([])

    @flask_app.route("/api/unnotified")
    def api_unnotified():
        try:
            conn = _get_db()
            dynamics = [dict(r) for r in conn.execute(
                "SELECT * FROM seen_dynamics WHERE notified = 0 ORDER BY first_seen_ts DESC LIMIT 50"
            ).fetchall()]
            videos = [dict(r) for r in conn.execute(
                "SELECT * FROM seen_videos WHERE notified = 0 ORDER BY first_seen_ts DESC LIMIT 50"
            ).fetchall()]
            conn.close()
            return jsonify({
                "dynamics": dynamics,
                "videos": videos,
                "dynamics_count": len(dynamics),
                "videos_count": len(videos),
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @flask_app.route("/api/videos")
    def api_videos():
        try:
            conn = _get_db()
            uid = request.args.get("uid")
            limit = request.args.get("limit", 50, type=int)
            if uid:
                rows = conn.execute("SELECT * FROM seen_videos WHERE uid = ? ORDER BY first_seen_ts DESC LIMIT ?", (uid, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM seen_videos ORDER BY first_seen_ts DESC LIMIT ?", (limit,)).fetchall()
            conn.close()
            return jsonify([dict(r) for r in rows])
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @flask_app.route("/api/dynamics")
    def api_dynamics():
        try:
            conn = _get_db()
            uid = request.args.get("uid")
            limit = request.args.get("limit", 50, type=int)
            if uid:
                rows = conn.execute("SELECT * FROM seen_dynamics WHERE uid = ? ORDER BY first_seen_ts DESC LIMIT ?", (uid, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM seen_dynamics ORDER BY first_seen_ts DESC LIMIT ?", (limit,)).fetchall()
            conn.close()
            return jsonify([dict(r) for r in rows])
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @flask_app.route("/api/notify-groups", methods=["GET"])
    def api_get_groups():
        try:
            conn = _get_db()
            rows = conn.execute("SELECT id, group_name, webhook_url, webhook_secret FROM notify_groups ORDER BY id").fetchall()
            groups = []
            for r in rows:
                g = dict(r)
                members = conn.execute("SELECT uid FROM notify_group_members WHERE group_id = ?", (g["id"],)).fetchall()
                g["members"] = [m["uid"] for m in members]
                g["member_info"] = []
                for m in members:
                    uid = normalize_bilibili_uid(m["uid"])
                    info = _get_user(uid)
                    g["member_info"].append({"uid": uid, "name": info.get("name", f"UID:{uid}")})
                groups.append(g)
            conn.close()
            return jsonify(groups)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @flask_app.route("/api/notify-groups", methods=["POST"])
    def api_create_group():
        data = request.get_json(force=True)
        name = str(data.get("group_name", "")).strip()
        url = str(data.get("webhook_url", "")).strip()
        secret = str(data.get("webhook_secret", "")).strip()
        if not name or not url:
            return jsonify({"error": "name and url required"}), 400
        try:
            conn = _get_db()
            cur = conn.execute("INSERT INTO notify_groups (group_name, webhook_url, webhook_secret, created_ts) VALUES (?, ?, ?, ?)", (name, url, secret, time.time()))
            conn.commit()
            gid = cur.lastrowid
            conn.close()
            return jsonify({"ok": True, "group": {"id": gid, "group_name": name}}), 201
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @flask_app.route("/api/notify-groups/<int:gid>", methods=["DELETE"])
    def api_delete_group(gid):
        try:
            conn = _get_db()
            conn.execute("DELETE FROM notify_group_members WHERE group_id = ?", (gid,))
            conn.execute("DELETE FROM notify_groups WHERE id = ?", (gid,))
            conn.commit()
            conn.close()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @flask_app.route("/api/notify-groups/<int:gid>", methods=["PUT"])
    def api_update_group(gid):
        data = request.get_json(force=True)
        group_name = data.get("group_name")
        webhook_url = data.get("webhook_url")
        webhook_secret = data.get("webhook_secret")
        try:
            conn = _get_db()
            sets = []
            vals = []
            if group_name is not None:
                sets.append("group_name = ?")
                vals.append(str(group_name))
            if webhook_url is not None:
                sets.append("webhook_url = ?")
                vals.append(str(webhook_url))
            if webhook_secret is not None:
                sets.append("webhook_secret = ?")
                vals.append(str(webhook_secret))
            if not sets:
                conn.close()
                return jsonify({"error": "No fields to update"}), 400
            vals.append(gid)
            conn.execute(f"UPDATE notify_groups SET {', '.join(sets)} WHERE id = ?", vals)
            conn.commit()
            conn.close()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @flask_app.route("/api/notify-groups/<int:gid>/members", methods=["POST"])
    def api_add_member(gid):
        data = request.get_json(force=True)
        uid = normalize_bilibili_uid(data.get("uid", ""))
        if not uid:
            return jsonify({"error": "uid required"}), 400
        try:
            conn = _get_db()
            conn.execute("INSERT OR IGNORE INTO notify_group_members (group_id, uid) VALUES (?, ?)", (gid, uid))
            conn.commit()
            conn.close()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @flask_app.route("/api/notify-groups/<int:gid>/members/<uid>", methods=["DELETE"])
    def api_remove_member(gid, uid):
        try:
            uid = normalize_bilibili_uid(uid)
            conn = _get_db()
            conn.execute("DELETE FROM notify_group_members WHERE group_id = ? AND uid = ?", (gid, uid))
            conn.commit()
            conn.close()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @flask_app.route("/api/test-feishu", methods=["POST"])
    def api_test_feishu():
        _reload_runtime_config_from_env()
        data = request.get_json(force=True)
        wurl = str(data.get("webhook_url", "")).strip()
        wsecret = str(data.get("webhook_secret", "")).strip()
        group_id = data.get("group_id")
        image_url = str(data.get("image_url", "")).strip()
        if not wurl and group_id:
            try:
                conn = _get_db()
                row = conn.execute("SELECT webhook_url, webhook_secret FROM notify_groups WHERE id = ?", (int(group_id),)).fetchone()
                if row:
                    wurl = row["webhook_url"]
                    wsecret = row["webhook_secret"]
                conn.close()
            except Exception:
                pass
        if not wurl:
            wurl = config.feishu_webhook_url
            wsecret = config.feishu_webhook_secret
        if not wurl:
            return jsonify({"ok": False, "error": "未配置 Webhook URL，请先在系统配置中填写飞书 Webhook 地址"}), 400
        try:
            from notifier import FeishuNotifier
            fn = FeishuNotifier(wurl, wsecret, app_id=config.feishu_app_id, app_secret=config.feishu_app_secret)
            image_url_used = image_url or _find_recent_image_url()
            result = fn.send_interactive_card(
                title="Bili Monitor - Test",
                content_lines=[
                    "**This is a test message**",
                    "If you see this, the Feishu bot is configured correctly!",
                    "This test also verifies Feishu image upload when a recent image is available.",
                ],
                header_color="turquoise",
                image_urls=[image_url_used] if image_url_used else [],
            )
            image_errors = getattr(fn, "last_image_upload_errors", [])
            if result.get("code") == 0:
                return jsonify({
                    "ok": True,
                    "msg": "Test message sent successfully",
                    "image_url_used": image_url_used,
                    "image_upload_errors": image_errors,
                    "image_uploaded": bool(image_url_used and not image_errors),
                    "has_app_auth": bool(config.feishu_app_id and config.feishu_app_secret),
                })
            else:
                return jsonify({
                    "ok": False,
                    "error": f"飞书返回错误: {result.get('msg', 'unknown')}",
                    "detail": result,
                    "image_url_used": image_url_used,
                    "image_upload_errors": image_errors,
                    "has_app_auth": bool(config.feishu_app_id and config.feishu_app_secret),
                })
        except Exception as e:
            return jsonify({"ok": False, "error": f"发送失败: {str(e)}"}), 500

    @flask_app.route("/api/cleanup", methods=["POST"])
    def api_cleanup():
        try:
            cutoff = time.time() - config.retention_days * 86400
            conn = _get_db()
            dyn_result = conn.execute("DELETE FROM seen_dynamics WHERE first_seen_ts < ?", (cutoff,))
            vid_result = conn.execute("DELETE FROM seen_videos WHERE first_seen_ts < ?", (cutoff,))
            conn.commit()
            conn.close()
            return jsonify({
                "ok": True,
                "dynamics_deleted": dyn_result.rowcount,
                "videos_deleted": vid_result.rowcount,
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @flask_app.route("/api/seen")
    def api_seen():
        try:
            limit = request.args.get("limit", 30, type=int)
            conn = _get_db()
            videos = [dict(r) for r in conn.execute(
                "SELECT * FROM seen_videos ORDER BY first_seen_ts DESC LIMIT ?", (limit,)
            ).fetchall()]
            dynamics = [dict(r) for r in conn.execute(
                "SELECT * FROM seen_dynamics ORDER BY first_seen_ts DESC LIMIT ?", (limit,)
            ).fetchall()]
            conn.close()
            return jsonify({"videos": videos, "dynamics": dynamics})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    for uid in config.target_uids:
        try:
            _get_user(uid)
        except Exception:
            pass

    return flask_app

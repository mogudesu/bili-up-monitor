"""SQLite-based state storage for Bilibili UP Monitor.

Tracks seen dynamic IDs and video BVIDs to prevent duplicate notifications.
"""

from __future__ import annotations

import sqlite3
import time
import json
import re
from pathlib import Path
from typing import Any, Optional


CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS seen_dynamics (
    dynamic_id TEXT PRIMARY KEY,
    uid TEXT NOT NULL,
    author_name TEXT,
    dynamic_type TEXT,
    title TEXT DEFAULT '',
    text_preview TEXT,
    full_text TEXT DEFAULT '',
    image_urls TEXT DEFAULT '[]',
    pub_ts INTEGER DEFAULT 0,
    attached_bvid TEXT,
    link TEXT,
    first_seen_ts REAL NOT NULL,
    notified INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS seen_videos (
    bvid TEXT PRIMARY KEY,
    uid TEXT NOT NULL,
    title TEXT,
    link TEXT,
    pub_ts INTEGER,
    pic TEXT DEFAULT '',
    author_name TEXT DEFAULT '',
    first_seen_ts REAL NOT NULL,
    notified INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS monitor_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dynamics_uid ON seen_dynamics(uid);
CREATE INDEX IF NOT EXISTS idx_videos_uid ON seen_videos(uid);
CREATE INDEX IF NOT EXISTS idx_dynamics_first_seen ON seen_dynamics(first_seen_ts);
CREATE INDEX IF NOT EXISTS idx_videos_first_seen ON seen_videos(first_seen_ts);

CREATE TABLE IF NOT EXISTS notify_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_name TEXT NOT NULL,
    webhook_url TEXT NOT NULL,
    webhook_secret TEXT DEFAULT '',
    created_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS notify_group_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    uid TEXT NOT NULL,
    FOREIGN KEY (group_id) REFERENCES notify_groups(id),
    UNIQUE(group_id, uid)
);

CREATE INDEX IF NOT EXISTS idx_group_members_uid ON notify_group_members(uid);
CREATE INDEX IF NOT EXISTS idx_group_members_group ON notify_group_members(group_id);

CREATE TABLE IF NOT EXISTS check_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    ts REAL NOT NULL,
    new_videos INTEGER DEFAULT 0,
    new_dynamics INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    summary TEXT DEFAULT ''
);
"""


def normalize_bilibili_uid(value: Any) -> str:
    """Normalize dashboard/group UID input to the numeric Bilibili UID."""
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"(?:space\.bilibili\.com|m\.bilibili\.com/space|bilibili\.com/space)/(\d+)", text)
    if match:
        return match.group(1)
    match = re.search(r"(?:^|\b)UID\s*[:：]\s*(\d+)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.fullmatch(r"\D*(\d+)\D*", text)
    if match:
        return match.group(1)
    return text


class MonitorStore:
    """SQLite-backed state store for dedup and monitoring state."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(CREATE_TABLES_SQL)
            self._ensure_schema()
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._conn
        if conn is None:
            return
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(seen_dynamics)").fetchall()}
        if "full_text" not in existing:
            conn.execute("ALTER TABLE seen_dynamics ADD COLUMN full_text TEXT DEFAULT ''")
        if "image_urls" not in existing:
            conn.execute("ALTER TABLE seen_dynamics ADD COLUMN image_urls TEXT DEFAULT '[]'")
        if "title" not in existing:
            conn.execute("ALTER TABLE seen_dynamics ADD COLUMN title TEXT DEFAULT ''")
        if "pub_ts" not in existing:
            conn.execute("ALTER TABLE seen_dynamics ADD COLUMN pub_ts INTEGER DEFAULT 0")
        video_cols = {row["name"] for row in conn.execute("PRAGMA table_info(seen_videos)").fetchall()}
        if "pic" not in video_cols:
            conn.execute("ALTER TABLE seen_videos ADD COLUMN pic TEXT DEFAULT ''")
        if "author_name" not in video_cols:
            conn.execute("ALTER TABLE seen_videos ADD COLUMN author_name TEXT DEFAULT ''")
        self._normalize_notify_group_members()
        conn.commit()

    def _normalize_notify_group_members(self) -> None:
        conn = self._conn
        if conn is None:
            return
        rows = conn.execute("SELECT id, group_id, uid FROM notify_group_members").fetchall()
        for row in rows:
            uid = row["uid"]
            normalized = normalize_bilibili_uid(uid)
            if not normalized or normalized == uid:
                continue
            conn.execute("DELETE FROM notify_group_members WHERE id = ?", (row["id"],))
            conn.execute(
                "INSERT OR IGNORE INTO notify_group_members (group_id, uid) VALUES (?, ?)",
                (row["group_id"], normalized),
            )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- Dynamics ----

    def is_dynamic_seen(self, dynamic_id: str) -> bool:
        conn = self._connect()
        row = conn.execute(
            "SELECT 1 FROM seen_dynamics WHERE dynamic_id = ?", (dynamic_id,)
        ).fetchone()
        return row is not None

    def mark_dynamic_seen(
        self,
        dynamic_id: str,
        uid: str,
        author_name: str = "",
        dynamic_type: str = "",
        title: str = "",
        text_preview: str = "",
        full_text: str = "",
        image_urls: Optional[list[str]] = None,
        pub_ts: int = 0,
        attached_bvid: str = "",
        link: str = "",
    ) -> bool:
        """Mark a dynamic as seen. Returns True if this is a new entry."""
        if self.is_dynamic_seen(dynamic_id):
            conn = self._connect()
            conn.execute(
                """UPDATE seen_dynamics
                   SET author_name = COALESCE(NULLIF(?, ''), author_name),
                       dynamic_type = COALESCE(NULLIF(?, ''), dynamic_type),
                       title = COALESCE(NULLIF(?, ''), title),
                       text_preview = COALESCE(NULLIF(?, ''), text_preview),
                       full_text = COALESCE(NULLIF(?, ''), full_text),
                       image_urls = CASE WHEN ? != '[]' THEN ? ELSE image_urls END,
                       pub_ts = CASE WHEN ? > 0 THEN ? ELSE pub_ts END,
                       attached_bvid = COALESCE(NULLIF(?, ''), attached_bvid),
                       link = COALESCE(NULLIF(?, ''), link)
                   WHERE dynamic_id = ?""",
                (
                    author_name,
                    dynamic_type,
                    title,
                    text_preview,
                    full_text,
                    json.dumps(image_urls or [], ensure_ascii=False),
                    json.dumps(image_urls or [], ensure_ascii=False),
                    int(pub_ts or 0),
                    int(pub_ts or 0),
                    attached_bvid,
                    link,
                    dynamic_id,
                ),
            )
            conn.commit()
            return False
        conn = self._connect()
        conn.execute(
            """INSERT OR IGNORE INTO seen_dynamics
               (dynamic_id, uid, author_name, dynamic_type, title, text_preview, full_text, image_urls, pub_ts, attached_bvid, link, first_seen_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dynamic_id, uid, author_name, dynamic_type, title, text_preview, full_text,
                json.dumps(image_urls or [], ensure_ascii=False), int(pub_ts or 0), attached_bvid, link, time.time(),
            ),
        )
        conn.commit()
        return True

    def mark_dynamic_notified(self, dynamic_id: str) -> None:
        conn = self._connect()
        conn.execute(
            "UPDATE seen_dynamics SET notified = 1 WHERE dynamic_id = ?", (dynamic_id,)
        )
        conn.commit()

    def get_unnotified_dynamics(
        self,
        uid: Optional[str] = None,
        limit: int = 50,
        since_pub_ts: Optional[int] = None,
        since_first_seen_ts: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        conditions = ["notified = 0"]
        values: list[Any] = []
        if uid:
            conditions.append("uid = ?")
            values.append(uid)
        if since_pub_ts is not None and since_first_seen_ts is not None:
            conditions.append("(pub_ts >= ? OR first_seen_ts >= ?)")
            values.extend([int(since_pub_ts), float(since_first_seen_ts)])
        elif since_pub_ts is not None:
            conditions.append("pub_ts >= ?")
            values.append(int(since_pub_ts))
        elif since_first_seen_ts is not None:
            conditions.append("first_seen_ts >= ?")
            values.append(float(since_first_seen_ts))
        where = " AND ".join(conditions)
        values.append(limit)
        rows = conn.execute(
            f"SELECT * FROM seen_dynamics WHERE {where} ORDER BY pub_ts DESC, first_seen_ts DESC LIMIT ?",
            values,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_unnotified_dynamics_legacy(self, uid: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._connect()
        if uid:
            rows = conn.execute(
                "SELECT * FROM seen_dynamics WHERE uid = ? AND notified = 0 ORDER BY first_seen_ts DESC LIMIT ?",
                (uid, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM seen_dynamics WHERE notified = 0 ORDER BY first_seen_ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- Videos ----

    def is_video_seen(self, bvid: str) -> bool:
        conn = self._connect()
        row = conn.execute(
            "SELECT 1 FROM seen_videos WHERE bvid = ?", (bvid,)
        ).fetchone()
        return row is not None

    def mark_video_seen(
        self,
        bvid: str,
        uid: str,
        title: str = "",
        link: str = "",
        pub_ts: int = 0,
        pic: str = "",
        author_name: str = "",
    ) -> bool:
        """Mark a video as seen. Returns True if this is a new entry."""
        if self.is_video_seen(bvid):
            return False
        conn = self._connect()
        conn.execute(
            """INSERT OR IGNORE INTO seen_videos
               (bvid, uid, title, link, pub_ts, pic, author_name, first_seen_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (bvid, uid, title, link, pub_ts, pic, author_name, time.time()),
        )
        conn.commit()
        return True

    def mark_video_notified(self, bvid: str) -> None:
        conn = self._connect()
        conn.execute(
            "UPDATE seen_videos SET notified = 1 WHERE bvid = ?", (bvid,)
        )
        conn.commit()

    def get_unnotified_videos(
        self,
        uid: Optional[str] = None,
        limit: int = 50,
        since_pub_ts: Optional[int] = None,
        since_first_seen_ts: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        conditions = ["notified = 0"]
        values: list[Any] = []
        if uid:
            conditions.append("uid = ?")
            values.append(uid)
        if since_pub_ts is not None and since_first_seen_ts is not None:
            conditions.append("(pub_ts >= ? OR first_seen_ts >= ?)")
            values.extend([int(since_pub_ts), float(since_first_seen_ts)])
        elif since_pub_ts is not None:
            conditions.append("pub_ts >= ?")
            values.append(int(since_pub_ts))
        elif since_first_seen_ts is not None:
            conditions.append("first_seen_ts >= ?")
            values.append(float(since_first_seen_ts))
        where = " AND ".join(conditions)
        values.append(limit)
        rows = conn.execute(
            f"SELECT * FROM seen_videos WHERE {where} ORDER BY first_seen_ts DESC LIMIT ?",
            values,
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- Monitor State ----

    def get_state(self, key: str, default: str = "") -> str:
        conn = self._connect()
        row = conn.execute(
            "SELECT value FROM monitor_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        conn = self._connect()
        conn.execute(
            """INSERT OR REPLACE INTO monitor_state (key, value, updated_ts)
               VALUES (?, ?, ?)""",
            (key, value, time.time()),
        )
        conn.commit()

    def get_last_check_time(self, uid: str) -> float:
        """Get the timestamp of the last successful check for a UID."""
        val = self.get_state(f"last_check:{uid}", "0")
        return float(val)

    def set_last_check_time(self, uid: str, ts: float) -> None:
        self.set_state(f"last_check:{uid}", str(ts))

    # ---- Cleanup ----

    def cleanup_old_records(self, retention_days: int) -> dict[str, int]:
        """Remove records older than retention_days. Returns counts of deleted rows."""
        cutoff = time.time() - retention_days * 86400
        conn = self._connect()
        dyn_result = conn.execute(
            "DELETE FROM seen_dynamics WHERE first_seen_ts < ?", (cutoff,)
        )
        vid_result = conn.execute(
            "DELETE FROM seen_videos WHERE first_seen_ts < ?", (cutoff,)
        )
        conn.commit()
        return {
            "dynamics_deleted": dyn_result.rowcount,
            "videos_deleted": vid_result.rowcount,
        }

    # ---- Notify Groups ----

    def create_notify_group(self, group_name: str, webhook_url: str, webhook_secret: str = "") -> dict[str, Any]:
        conn = self._connect()
        cursor = conn.execute(
            """INSERT INTO notify_groups (group_name, webhook_url, webhook_secret, created_ts)
               VALUES (?, ?, ?, ?)""",
            (group_name, webhook_url, webhook_secret, time.time()),
        )
        conn.commit()
        group_id = cursor.lastrowid
        return {"id": group_id, "group_name": group_name, "webhook_url": webhook_url, "webhook_secret": webhook_secret}

    def get_notify_groups(self) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT id, group_name, webhook_url, webhook_secret, created_ts FROM notify_groups ORDER BY id"
        ).fetchall()
        groups = []
        for r in rows:
            group = dict(r)
            members = conn.execute(
                "SELECT uid FROM notify_group_members WHERE group_id = ?", (group["id"],)
            ).fetchall()
            group["members"] = [m["uid"] for m in members]
            groups.append(group)
        return groups

    def get_notify_group(self, group_id: int) -> Optional[dict[str, Any]]:
        conn = self._connect()
        row = conn.execute(
            "SELECT id, group_name, webhook_url, webhook_secret, created_ts FROM notify_groups WHERE id = ?",
            (group_id,),
        ).fetchone()
        if not row:
            return None
        group = dict(row)
        members = conn.execute(
            "SELECT uid FROM notify_group_members WHERE group_id = ?", (group_id,)
        ).fetchall()
        group["members"] = [m["uid"] for m in members]
        return group

    def update_notify_group(self, group_id: int, group_name: Optional[str] = None,
                            webhook_url: Optional[str] = None, webhook_secret: Optional[str] = None) -> bool:
        conn = self._connect()
        updates = []
        values = []
        if group_name is not None:
            updates.append("group_name = ?")
            values.append(group_name)
        if webhook_url is not None:
            updates.append("webhook_url = ?")
            values.append(webhook_url)
        if webhook_secret is not None:
            updates.append("webhook_secret = ?")
            values.append(webhook_secret)
        if not updates:
            return False
        values.append(group_id)
        result = conn.execute(
            f"UPDATE notify_groups SET {', '.join(updates)} WHERE id = ?", values
        )
        conn.commit()
        return result.rowcount > 0

    def delete_notify_group(self, group_id: int) -> bool:
        conn = self._connect()
        conn.execute("DELETE FROM notify_group_members WHERE group_id = ?", (group_id,))
        result = conn.execute("DELETE FROM notify_groups WHERE id = ?", (group_id,))
        conn.commit()
        return result.rowcount > 0

    def add_group_member(self, group_id: int, uid: str) -> bool:
        conn = self._connect()
        uid = normalize_bilibili_uid(uid)
        if not uid:
            return False
        try:
            conn.execute(
                "INSERT OR IGNORE INTO notify_group_members (group_id, uid) VALUES (?, ?)",
                (group_id, uid),
            )
            conn.commit()
            return True
        except Exception:
            return False

    def remove_group_member(self, group_id: int, uid: str) -> bool:
        conn = self._connect()
        uid = normalize_bilibili_uid(uid)
        result = conn.execute(
            "DELETE FROM notify_group_members WHERE group_id = ? AND uid = ?",
            (group_id, uid),
        )
        conn.commit()
        return result.rowcount > 0

    def get_group_for_uid(self, uid: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        uid = normalize_bilibili_uid(uid)
        row = conn.execute(
            """SELECT ng.id, ng.group_name, ng.webhook_url, ng.webhook_secret
               FROM notify_groups ng
               JOIN notify_group_members ngm ON ng.id = ngm.group_id
               WHERE ngm.uid = ?""",
            (uid,),
        ).fetchone()
        return dict(row) if row else None

    def add_check_history(self, timestamp: str, ts: float, new_videos: int, new_dynamics: int, errors: int, summary: str = "") -> None:
        conn = self._connect()
        conn.execute(
            """INSERT INTO check_history (timestamp, ts, new_videos, new_dynamics, errors, summary)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (timestamp, ts, new_videos, new_dynamics, errors, summary),
        )
        conn.commit()
        conn.execute("DELETE FROM check_history WHERE id NOT IN (SELECT id FROM check_history ORDER BY id DESC LIMIT 200)")
        conn.commit()

    def get_check_history(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, timestamp, ts, new_videos, new_dynamics, errors, summary FROM check_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

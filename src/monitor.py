"""Bilibili UP Monitor - Main monitor loop with configurable scheduling."""

from __future__ import annotations

import logging
import json
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from config import MonitorConfig, load_config
from fetcher import fetch_up_updates, fetch_user_info, check_cookie_status, get_cookies
from store import MonitorStore, normalize_bilibili_uid
from notifier import FeishuNotifier, LocalNotifier, Notifier

logger = logging.getLogger("bilibili-monitor")


class BilibiliMonitor:
    """Main monitor class that orchestrates fetching, dedup, and notification."""

    def __init__(self, config: MonitorConfig) -> None:
        self.config = config
        self.store = MonitorStore(config.resolve_path(config.db_path))
        self.notifier = self._build_notifier()
        self._running = False
        self._last_check: dict[str, float] = {}

        # User info cache
        self._user_cache: dict[str, dict[str, Any]] = {}

    def _build_notifier(self) -> Notifier:
        feishu = None
        local = None
        group_notifiers: dict[int, FeishuNotifier] = {}
        uid_group_map: dict[str, int] = {}

        if self.config.enable_feishu and self.config.feishu_webhook_url:
            feishu = FeishuNotifier(
                self.config.feishu_webhook_url,
                self.config.feishu_webhook_secret,
                self.config.feishu_app_id,
                self.config.feishu_app_secret,
            )
            logger.info("Feishu notifier enabled (default)")
        else:
            logger.info(
                f"Default Feishu notifier disabled: enable_feishu={self.config.enable_feishu}, "
                f"has_webhook={bool(self.config.feishu_webhook_url)}"
            )

        try:
            groups = self.store.get_notify_groups()
            for group in groups:
                gid = group["id"]
                webhook_url = group.get("webhook_url", "")
                webhook_secret = group.get("webhook_secret", "")
                if webhook_url:
                    group_notifiers[gid] = FeishuNotifier(
                        webhook_url,
                        webhook_secret,
                        self.config.feishu_app_id,
                        self.config.feishu_app_secret,
                    )
                    for uid in group.get("members", []):
                        normalized_uid = normalize_bilibili_uid(uid)
                        if normalized_uid:
                            uid_group_map[normalized_uid] = gid
                    logger.info(
                        f"Feishu group '{group['group_name']}' (ID:{gid}) enabled "
                        f"with {len(group.get('members', []))} members: {group.get('members', [])}"
                    )
                else:
                    logger.info(
                        f"Feishu group '{group['group_name']}' (ID:{gid}) skipped: no webhook_url"
                    )
        except Exception as exc:
            logger.warning(f"Failed to load notify groups: {exc}")

        logger.info(f"Notifier built: uid_group_map={uid_group_map}, groups={list(group_notifiers.keys())}")

        if self.config.enable_local_output:
            local = LocalNotifier(self.config.resolve_path(self.config.local_output_dir))
            logger.info(f"Local notifier enabled: {self.config.resolve_path(self.config.local_output_dir)}")

        return Notifier(
            feishu=feishu,
            local=local,
            group_notifiers=group_notifiers,
            uid_group_map=uid_group_map,
        )

    def reload_notifier(self) -> None:
        """Reload notifier config (e.g. after group changes in DB)."""
        self.notifier = self._build_notifier()
        logger.info("Notifier reloaded with updated group configuration")

    def _get_user_info(self, uid: str) -> dict[str, Any]:
        """Get user info with caching."""
        if uid not in self._user_cache:
            try:
                self._user_cache[uid] = fetch_user_info(uid)
            except Exception as exc:
                logger.warning(f"Failed to fetch user info for UID {uid}: {exc}")
                self._user_cache[uid] = {
                    "uid": uid,
                    "name": f"UID:{uid}",
                    "space_url": f"https://space.bilibili.com/{uid}",
                    "dynamic_url": f"https://space.bilibili.com/{uid}/dynamic",
                }
        return self._user_cache[uid]

    def _can_mark_notified(self, success: bool) -> bool:
        """Keep rows unnotified when Feishu is enabled but unavailable."""
        if not success:
            return False
        if self.config.enable_feishu and not self.notifier.has_any_feishu():
            logger.warning("Feishu is enabled but no webhook/group is available; keeping row unnotified")
            return False
        return True

    def _retry_unnotified_recent(
        self,
        uid: str,
        author_name: str,
        recent_cutoff: int,
        attempted_bvids: set[str] | None = None,
        attempted_dynamic_ids: set[str] | None = None,
        include_recently_seen: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        attempted_bvids = attempted_bvids or set()
        attempted_dynamic_ids = attempted_dynamic_ids or set()
        first_seen_cutoff = float(recent_cutoff) if include_recently_seen else None

        retried_videos: list[dict[str, Any]] = []
        for row in self.store.get_unnotified_videos(
            uid=uid,
            since_pub_ts=recent_cutoff,
            since_first_seen_ts=first_seen_cutoff,
        ):
            bvid = row.get("bvid", "")
            if not bvid or bvid in attempted_bvids:
                continue
            retry_video_link = row.get("link", "")
            if not retry_video_link or "space.bilibili.com" in retry_video_link:
                retry_video_link = f"https://www.bilibili.com/video/{bvid}/"
            retry_video = {
                "bvid": bvid,
                "title": row.get("title", ""),
                "link": retry_video_link,
                "author_name": row.get("author_name") or author_name,
                "author_mid": row.get("uid") or uid,
                "pub_ts": row.get("pub_ts") or 0,
                "pic": row.get("pic", ""),
                "source": "retry_unnotified_recent",
            }
            success = self.notifier.notify_new_video(retry_video, retry_video["author_name"])
            if self._can_mark_notified(success):
                self.store.mark_video_notified(bvid)
                retried_videos.append(retry_video)
            else:
                logger.warning(f"Video {bvid} retry notification failed")

        retried_dynamics: list[dict[str, Any]] = []
        for row in self.store.get_unnotified_dynamics(
            uid=uid,
            since_pub_ts=recent_cutoff,
            since_first_seen_ts=first_seen_cutoff,
        ):
            dynamic_id = row.get("dynamic_id", "")
            if not dynamic_id or dynamic_id in attempted_dynamic_ids:
                continue
            try:
                image_urls = json.loads(row.get("image_urls") or "[]")
                if not isinstance(image_urls, list):
                    image_urls = []
            except Exception:
                image_urls = []
            retry_link = row.get("link", "")
            if not retry_link or "space.bilibili.com" in retry_link:
                retry_link = f"https://www.bilibili.com/opus/{dynamic_id}"
            retry_dynamic = {
                "dynamic_id": dynamic_id,
                "type": row.get("dynamic_type", ""),
                "title": row.get("title", ""),
                "author_name": row.get("author_name") or author_name,
                "author_mid": row.get("uid") or uid,
                "pub_ts": row.get("pub_ts") or 0,
                "link": retry_link,
                "text": row.get("full_text") or row.get("text_preview") or "",
                "attached_video": None,
                "images": image_urls,
                "source": "retry_unnotified_recent",
            }
            success = self.notifier.notify_new_dynamic(retry_dynamic, retry_dynamic["author_name"])
            if self._can_mark_notified(success):
                self.store.mark_dynamic_notified(dynamic_id)
                retried_dynamics.append(retry_dynamic)
            else:
                logger.warning(f"Dynamic {dynamic_id} retry notification failed")

        return {
            "retried_videos": retried_videos,
            "retried_dynamics": retried_dynamics,
        }

    def retry_unnotified_all(self, lookback_hours: int = 24) -> list[dict[str, Any]]:
        """Retry delivery for previously seen but unnotified recent rows."""
        recent_cutoff = int(time.time()) - int(lookback_hours * 3600)
        results: list[dict[str, Any]] = []
        for uid in self.config.target_uids:
            user_info = self._get_user_info(uid)
            author_name = user_info.get("name", f"UID:{uid}")
            retry_result = self._retry_unnotified_recent(
                uid,
                author_name,
                recent_cutoff,
                include_recently_seen=True,
            )
            results.append({
                "uid": uid,
                "author_name": author_name,
                **retry_result,
            })
        return results

    def check_single_up(self, uid: str) -> dict[str, Any]:
        """Run one check cycle for a single UP account.

        Returns a result dict with new_videos and new_dynamics lists.
        """
        user_info = self._get_user_info(uid)
        author_name = user_info.get("name", f"UID:{uid}")

        # Determine lookback window
        now_ts = int(time.time())
        recent_cutoff = now_ts - 86400
        last_check = self.store.get_last_check_time(uid)
        # Always fetch the trailing 24 hours and let SQLite dedupe already-seen
        # rows. Advancing last_check after a partial fetch must not make a later
        # successful fetch skip valid recent videos or dynamics.
        since_ts = recent_cutoff

        logger.info(f"Checking {author_name} (UID:{uid}), since={since_ts}, last_check={last_check}")

        # Fetch updates
        try:
            result = fetch_up_updates(uid, since_ts, author_name=author_name)
        except Exception as exc:
            logger.error(f"Failed to fetch updates for UID {uid}: {exc}")
            retry_result = self._retry_unnotified_recent(uid, author_name, recent_cutoff)
            return {
                "uid": uid,
                "author_name": author_name,
                "new_videos": [],
                "new_dynamics": [],
                **retry_result,
                "error": str(exc),
            }

        # Process videos - find new ones
        new_videos: list[dict[str, Any]] = []
        for video in result.get("videos", []):
            bvid = video.get("bvid", "")
            if not bvid:
                continue
            pub_ts = int(video.get("pub_ts") or 0)
            if pub_ts and pub_ts < since_ts:
                logger.debug(f"Skipping video {bvid}: pub_ts={pub_ts} < since_ts={since_ts}")
                continue
            is_new = self.store.mark_video_seen(
                bvid=bvid,
                uid=uid,
                title=video.get("title", ""),
                link=video.get("link", ""),
                pub_ts=pub_ts,
                pic=video.get("pic", ""),
                author_name=author_name,
            )
            if is_new:
                video["author_name"] = author_name
                if not video.get("author_mid"):
                    video["author_mid"] = uid
                new_videos.append(video)

        # Process dynamics - find new ones
        new_dynamics: list[dict[str, Any]] = []
        for dynamic in result.get("dynamics", []):
            dynamic_id = dynamic.get("dynamic_id", "")
            if not dynamic_id:
                continue
            pub_ts = dynamic.get("pub_ts")
            if pub_ts is not None:
                pub_ts = int(pub_ts)
                if pub_ts < since_ts:
                    logger.debug(f"Skipping dynamic {dynamic_id}: pub_ts={pub_ts} < since_ts={since_ts}")
                    continue
                if pub_ts > now_ts + 300:
                    logger.debug(f"Skipping dynamic {dynamic_id}: pub_ts={pub_ts} is in the future")
                    continue
            else:
                logger.info(f"Skipping dynamic {dynamic_id}: no reliable pub_ts")
                continue
            text_preview = (dynamic.get("text") or "")[:100]
            attached_bvid = ""
            if dynamic.get("attached_video"):
                attached_bvid = dynamic.get("attached_video", {}).get("bvid", "")
            is_new = self.store.mark_dynamic_seen(
                dynamic_id=dynamic_id,
                uid=uid,
                author_name=author_name,
                dynamic_type=dynamic.get("type", ""),
                title=dynamic.get("title", ""),
                text_preview=text_preview,
                full_text=dynamic.get("text", ""),
                image_urls=dynamic.get("images", []),
                pub_ts=pub_ts,
                attached_bvid=attached_bvid,
                link=dynamic.get("link", ""),
            )
            if is_new:
                dynamic["author_name"] = author_name
                if not dynamic.get("author_mid"):
                    dynamic["author_mid"] = uid
                new_dynamics.append(dynamic)

        # Notify new videos
        attempted_bvids: set[str] = set()
        for video in new_videos:
            bvid = video.get("bvid", "")
            if bvid:
                attempted_bvids.add(bvid)
            success = self.notifier.notify_new_video(video, author_name)
            if self._can_mark_notified(success):
                self.store.mark_video_notified(bvid)
            else:
                logger.warning(f"Video {bvid} notification failed, will retry next check")

        # Notify new dynamics
        attempted_dynamic_ids: set[str] = set()
        for dynamic in new_dynamics:
            if dynamic.get("dynamic_id"):
                attempted_dynamic_ids.add(dynamic["dynamic_id"])
            success = self.notifier.notify_new_dynamic(dynamic, author_name)
            if self._can_mark_notified(success):
                if dynamic.get("dynamic_id"):
                    self.store.mark_dynamic_notified(dynamic["dynamic_id"])
            else:
                logger.warning(f"Dynamic {dynamic.get('dynamic_id')} notification failed, will retry next check")

        # Retry previously seen but unnotified dynamics, but only when their
        # publish time proves they are inside the trailing 24-hour window.
        retry_result = self._retry_unnotified_recent(
            uid,
            author_name,
            recent_cutoff,
            attempted_bvids=attempted_bvids,
            attempted_dynamic_ids=attempted_dynamic_ids,
        )
        retried_videos = retry_result["retried_videos"]
        retried_dynamics = retry_result["retried_dynamics"]

        # Update last check time
        now = time.time()
        self.store.set_last_check_time(uid, now)
        self._last_check[uid] = now

        # Log warnings
        for warning in result.get("warnings", []):
            logger.warning(f"UID {uid}: {warning}")

        logger.info(
            f"Check complete for {author_name}: "
            f"{len(new_videos)} new videos, {len(new_dynamics)} new dynamics, "
            f"{len(retried_videos)} retried videos, {len(retried_dynamics)} retried dynamics"
        )

        return {
            "uid": uid,
            "author_name": author_name,
            "new_videos": new_videos,
            "new_dynamics": new_dynamics,
            "retried_videos": retried_videos,
            "retried_dynamics": retried_dynamics,
            "warnings": result.get("warnings", []),
        }

    def check_all(self) -> list[dict[str, Any]]:
        """Run one check cycle for all monitored UP accounts."""
        results: list[dict[str, Any]] = []
        for uid in self.config.target_uids:
            try:
                result = self.check_single_up(uid)
                results.append(result)
            except Exception as exc:
                logger.error(f"Error checking UID {uid}: {exc}")
                results.append({
                    "uid": uid,
                    "author_name": self._get_user_info(uid).get("name", f"UID:{uid}"),
                    "new_videos": [],
                    "new_dynamics": [],
                    "error": str(exc),
                })

        # Send summary if there are new updates
        self.notifier.notify_check_summary(results)
        return results

    def cleanup(self) -> dict[str, int]:
        """Remove old records beyond retention period."""
        result = self.store.cleanup_old_records(self.config.retention_days)
        if result["dynamics_deleted"] > 0 or result["videos_deleted"] > 0:
            logger.info(
                f"Cleanup: removed {result['dynamics_deleted']} dynamics, "
                f"{result['videos_deleted']} videos"
            )
        return result

    def _check_cookie_health(self) -> None:
        """Periodically check cookie validity and log status."""
        cookies = get_cookies()
        if not cookies.get("SESSDATA"):
            return
        status = check_cookie_status()
        if status.get("is_logged_in"):
            logger.debug(f"Cookie health: OK - {status.get('message')}")
        else:
            logger.warning(f"Cookie health: {status.get('message')} - 请重新登录")

    def run_once(self) -> list[dict[str, Any]]:
        """Run a single check cycle and return results."""
        logger.info("=== Running single check cycle ===")
        results = self.check_all()
        self.cleanup()
        return results

    def run_loop(self) -> None:
        """Run the monitor in a continuous loop with the configured interval."""
        self._running = True
        interval = self.config.interval_seconds
        check_count = 0

        def _signal_handler(signum: int, frame: Any) -> None:
            logger.info("Received shutdown signal, stopping...")
            self._running = False

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        logger.info(
            f"Monitor started: {len(self.config.target_uids)} targets, "
            f"interval={interval}s ({interval // 60}m)"
        )

        # Print target list
        for uid in self.config.target_uids:
            info = self._get_user_info(uid)
            logger.info(f"  - {info.get('name', 'Unknown')} (UID:{uid})")
            logger.info(f"    Space: {info.get('space_url', '')}")
            logger.info(f"    Dynamic: {info.get('dynamic_url', '')}")

        while self._running:
            check_count += 1
            now = datetime.now(timezone(timedelta(hours=8)))
            logger.info(f"\n{'='*50}")
            logger.info(f"Check #{check_count} at {now.strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info(f"{'='*50}")

            try:
                self.check_all()
            except Exception as exc:
                logger.error(f"Check cycle failed: {exc}", exc_info=True)

            # Cleanup every 10 checks
            if check_count % 10 == 0:
                try:
                    self.cleanup()
                except Exception as exc:
                    logger.error(f"Cleanup failed: {exc}")

                try:
                    self._check_cookie_health()
                except Exception as exc:
                    logger.debug(f"Cookie health check failed: {exc}")

            # Sleep in small increments for responsive shutdown
            sleep_until = time.time() + interval
            while self._running and time.time() < sleep_until:
                time.sleep(min(5, sleep_until - time.time()))

        logger.info("Monitor stopped.")
        self.store.close()

    def stop(self) -> None:
        """Stop the monitor loop."""
        self._running = False


def setup_logging(config: MonitorConfig) -> None:
    """Configure logging with both console and file output."""
    log_dir = config.resolve_path("logs")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # Root logger
    root_logger = logging.getLogger("bilibili-monitor")
    root_logger.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    # Console handler (with safety check for Android)
    try:
        if sys.stdout and hasattr(sys.stdout, 'write'):
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))
            console_handler.setFormatter(logging.Formatter(log_format, date_format))
            root_logger.addHandler(console_handler)
    except Exception:
        pass

    # File handler (with safety check for Android)
    try:
        now = datetime.now(timezone(timedelta(hours=8)))
        log_file = log_dir / f"monitor_{now.strftime('%Y%m%d')}.log"
        file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(log_format, date_format))
        root_logger.addHandler(file_handler)
    except Exception:
        pass

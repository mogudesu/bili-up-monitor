"""Notification sink - Feishu bot webhook and local file output."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import base64
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    from store import normalize_bilibili_uid
except Exception:
    def normalize_bilibili_uid(value: Any) -> str:
        return str(value or "").strip()

logger = logging.getLogger("bilibili-monitor.notifier")


# ---- Feishu Bot Webhook ----

class FeishuNotifier:
    """Send notifications to Feishu via custom bot webhook."""

    def __init__(self, webhook_url: str, secret: str = "", app_id: str = "", app_secret: str = "") -> None:
        self.webhook_url = webhook_url
        self.secret = secret
        self.app_id = app_id
        self.app_secret = app_secret
        self._tenant_access_token = ""
        self._tenant_access_token_expires = 0
        self.last_image_upload_errors: list[str] = []

    def _build_sign(self, timestamp: str) -> str:
        """Build sign for Feishu webhook security check."""
        if not self.secret:
            return ""
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = base64.b64encode(hmac_code).decode("utf-8")
        return sign

    def _send_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON payload to the Feishu webhook."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "BilibiliMonitor/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result
        except urllib.error.URLError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc) or "SSL" in str(exc):
                logger.warning("SSL verify failed, retrying without certificate verification")
                try:
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                        result = json.loads(resp.read().decode("utf-8"))
                        return result
                except urllib.error.HTTPError as exc2:
                    error_body = exc2.read().decode("utf-8", errors="replace")
                    logger.error(f"Feishu webhook HTTP {exc2.code}: {error_body[:300]}")
                    return {"code": -1, "msg": f"HTTP {exc2.code}"}
                except Exception as exc2:
                    logger.error(f"Feishu webhook request failed (no SSL verify): {exc2}")
                    return {"code": -1, "msg": str(exc2)}
            if isinstance(exc, urllib.error.HTTPError):
                error_body = exc.read().decode("utf-8", errors="replace")
                logger.error(f"Feishu webhook HTTP {exc.code}: {error_body[:300]}")
                return {"code": -1, "msg": f"HTTP {exc.code}"}
            logger.error(f"Feishu webhook request failed: {exc}")
            return {"code": -1, "msg": str(exc)}
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            logger.error(f"Feishu webhook HTTP {exc.code}: {error_body[:300]}")
            return {"code": -1, "msg": f"HTTP {exc.code}"}
        except Exception as exc:
            logger.error(f"Feishu webhook request failed: {exc}")
            return {"code": -1, "msg": str(exc)}

    def _post_json(self, url: str, payload: dict[str, Any], headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers = {"Content-Type": "application/json", "User-Agent": "BilibiliMonitor/1.0"}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.URLError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc) or "SSL" in str(exc):
                logger.warning("SSL verify failed for _post_json, retrying without certificate verification")
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                    return json.loads(resp.read().decode("utf-8", errors="replace"))
            raise

    def _get_tenant_access_token(self) -> str:
        if not self.app_id or not self.app_secret:
            return ""
        now = int(time.time())
        if self._tenant_access_token and now < self._tenant_access_token_expires:
            return self._tenant_access_token
        result = self._post_json(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": self.app_id, "app_secret": self.app_secret},
        )
        if result.get("code") != 0:
            raise RuntimeError(f"Feishu tenant token failed: {result.get('msg') or result}")
        self._tenant_access_token = result.get("tenant_access_token", "")
        self._tenant_access_token_expires = now + int(result.get("expire", 7200)) - 600
        return self._tenant_access_token

    def _normalize_image_url(self, image_url: str) -> str:
        url = str(image_url or "").strip()
        if url.startswith("//"):
            return "https:" + url
        return url

    def _image_url_candidates(self, image_url: str) -> list[str]:
        """Return conservative Bilibili CDN candidates for Feishu image upload."""
        original = self._normalize_image_url(image_url)
        candidates: list[str] = []

        def add(url: str) -> None:
            if url and url not in candidates:
                candidates.append(url)

        add(original)
        no_query = original.split("?", 1)[0]
        add(no_query)
        no_at = no_query.split("@", 1)[0]
        add(no_at)

        lower = no_at.lower()
        for suffix in (".webp", ".avif"):
            if lower.endswith(suffix):
                add(no_at[: -len(suffix)] + ".jpg")
        return candidates

    def _download_image_bytes(self, image_url: str) -> tuple[bytes, str, str]:
        last_error = ""
        allowed_types = {"image/jpeg", "image/jpg", "image/png", "image/gif"}
        for candidate in self._image_url_candidates(image_url):
            img_req = urllib.request.Request(
                candidate,
                headers={
                    "User-Agent": "Mozilla/5.0 BilibiliMonitor/1.0",
                    "Referer": "https://www.bilibili.com/",
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                },
            )
            try:
                with urllib.request.urlopen(img_req, timeout=20) as resp:
                    image_bytes = resp.read()
                    content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip().lower()
            except urllib.error.URLError as exc:
                if "CERTIFICATE_VERIFY_FAILED" in str(exc) or "SSL" in str(exc):
                    logger.warning("SSL verify failed for image download, retrying without certificate verification")
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    with urllib.request.urlopen(img_req, timeout=20, context=ctx) as resp:
                        image_bytes = resp.read()
                        content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip().lower()
                else:
                    last_error = f"{candidate}: {exc}"
                    continue
            except Exception as exc:
                last_error = f"{candidate}: {exc}"
                continue

            if not image_bytes:
                last_error = f"{candidate}: empty image response"
                continue
            if content_type in allowed_types:
                return image_bytes, content_type, candidate
            last_error = f"{candidate}: unsupported image content type {content_type}"
        raise RuntimeError(last_error or "No usable image candidate")

    def upload_image_url(self, image_url: str) -> str:
        """Upload an image URL to Feishu and return image_key. Returns empty when app auth is not configured."""
        token = self._get_tenant_access_token()
        if not token:
            self.last_image_upload_errors.append("Feishu App ID/App Secret not configured; image upload skipped")
            return ""
        image_bytes, content_type, used_url = self._download_image_bytes(image_url)
        filename = "image.png" if content_type == "image/png" else "image.gif" if content_type == "image/gif" else "image.jpg"
        boundary = f"----BiliMonitor{int(time.time() * 1000)}"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="image_type"\r\n\r\n'
            "message\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8") + image_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/images",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
                "User-Agent": "BilibiliMonitor/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.URLError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc) or "SSL" in str(exc):
                logger.warning("SSL verify failed for image upload, retrying without certificate verification")
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                    result = json.loads(resp.read().decode("utf-8", errors="replace"))
            else:
                raise
        if result.get("code") != 0:
            raise RuntimeError(f"Feishu image upload failed for {used_url}: {result.get('msg') or result}")
        image_key = (result.get("data") or {}).get("image_key", "")
        if not image_key:
            raise RuntimeError(f"Feishu image upload returned empty image_key for {used_url}: {result}")
        return image_key

    def send_text(self, text: str) -> dict[str, Any]:
        """Send a plain text message."""
        payload: dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": text},
        }
        if self.secret:
            timestamp = str(round(time.time()))
            sign = self._build_sign(timestamp)
            payload["timestamp"] = timestamp
            payload["sign"] = sign
        return self._send_payload(payload)

    def send_rich_text(self, title: str, content: str, link: Optional[str] = None) -> dict[str, Any]:
        """Send a rich text (post) message."""
        post_content = []
        # Title line
        post_content.append([{"tag": "text", "text": title}])
        # Content lines
        for line in content.split("\n"):
            if line.strip():
                post_content.append([{"tag": "text", "text": line}])
        # Link
        if link:
            post_content.append([{"tag": "a", "text": "查看详情", "href": link}])

        payload: dict[str, Any] = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": title,
                        "content": post_content,
                    }
                }
            },
        }
        if self.secret:
            timestamp = str(round(time.time()))
            sign = self._build_sign(timestamp)
            payload["timestamp"] = timestamp
            payload["sign"] = sign
        return self._send_payload(payload)

    def send_interactive_card(
        self,
        title: str,
        content_lines: list[str],
        link: Optional[str] = None,
        link_text: str = "查看详情",
        header_color: str = "blue",
        image_urls: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Send an interactive card message."""
        self.last_image_upload_errors = []
        elements: list[dict[str, Any]] = []

        # Content markdown
        md_content = "\n".join(content_lines)
        if md_content.strip():
            elements.append({"tag": "markdown", "content": md_content})

        # Link button
        if link:
            elements.append({
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": link_text},
                        "type": "primary",
                        "url": link,
                    }
                ],
            })

        fallback_images: list[str] = []
        for image_url in (image_urls or [])[:6]:
            if not image_url:
                continue
            try:
                image_key = self.upload_image_url(image_url)
                if image_key:
                    elements.append({
                        "tag": "img",
                        "img_key": image_key,
                        "alt": {"tag": "plain_text", "content": "Bilibili image"},
                    })
                else:
                    # No app auth configured; keep the card valid and expose links.
                    fallback_images.append(image_url)
            except Exception as exc:
                msg = f"Feishu image upload failed, falling back to image links: {exc}"
                logger.warning(msg)
                self.last_image_upload_errors.append(msg)
                fallback_images.append(image_url)
        if fallback_images:
            md_images = "\n".join(
                f"[图片 {idx}]({url})"
                for idx, url in enumerate(fallback_images[:6], start=1)
            )
            elements.append({
                "tag": "markdown",
                "content": md_images,
            })

        # Divider
        elements.append({"tag": "hr"})

        payload: dict[str, Any] = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": header_color,
                },
                "elements": elements,
            },
        }
        if self.secret:
            timestamp = str(round(time.time()))
            sign = self._build_sign(timestamp)
            payload["timestamp"] = timestamp
            payload["sign"] = sign
        return self._send_payload(payload)


# ---- Local File Output ----

class LocalNotifier:
    """Write notifications as local Markdown files."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write_update(
        self,
        uid: str,
        author_name: str,
        videos: list[dict[str, Any]],
        dynamics: list[dict[str, Any]],
    ) -> Path:
        """Write a Markdown report for detected updates."""
        now = datetime.now(timezone(timedelta(hours=8)))
        filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{uid}.md"
        filepath = self.output_dir / filename

        lines: list[str] = []
        lines.append(f"# B站动态更新 - {author_name}")
        lines.append(f"\n> 检测时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"> UP主页: https://space.bilibili.com/{uid}")
        lines.append("")

        if videos:
            lines.append("## 新视频")
            lines.append("")
            for v in videos:
                lines.append(f"### [{v.get('title', '无标题')}]({v.get('link', '#')})")
                if v.get("pub_time_utc"):
                    lines.append(f"- 发布时间: {v['pub_time_utc']}")
                if v.get("description"):
                    desc = v["description"][:200]
                    lines.append(f"- 简介: {desc}")
                if v.get("play") is not None:
                    lines.append(f"- 播放量: {v['play']}")
                lines.append("")

        if dynamics:
            lines.append("## 新动态")
            lines.append("")
            for d in dynamics:
                dtype = d.get("type", "")
                title = d.get("title", "")
                text = d.get("text") or ""
                link = d.get("link") or ""
                images = d.get("images", [])
                heading = title if title else f"动态 {d.get('dynamic_id', '未知')}"
                lines.append(f"### {heading}")
                if dtype:
                    dtype_label = {
                        "DYNAMIC_TYPE_DRAW": "图文",
                        "DYNAMIC_TYPE_AV": "视频",
                        "DYNAMIC_TYPE_WORD": "文字",
                        "DYNAMIC_TYPE_FORWARD": "转发",
                    }.get(dtype, dtype)
                    lines.append(f"- 类型: {dtype_label}")
                if text:
                    lines.append(f"- 内容: {text[:500]}")
                if link:
                    lines.append(f"- 链接: {link}")
                if d.get("attached_video"):
                    av = d["attached_video"]
                    lines.append(f"- 关联视频: [{av.get('title', '')}]({av.get('link', '#')})")
                for img_url in images[:6]:
                    lines.append(f"- ![图片]({img_url})")
                lines.append("")

        if not videos and not dynamics:
            lines.append("*本次检查无新更新*")

        filepath.write_text("\n".join(lines), encoding="utf-8")
        return filepath


# ---- Unified Notifier ----

class Notifier:
    """Unified notification dispatcher supporting Feishu and local output.

    Supports per-UID Feishu bot routing via group_notifiers dict.
    group_notifiers maps group_id -> FeishuNotifier instance.
    uid_group_map maps uid -> group_id for quick lookup.
    """

    def __init__(
        self,
        feishu: Optional[FeishuNotifier] = None,
        local: Optional[LocalNotifier] = None,
        group_notifiers: Optional[dict[int, FeishuNotifier]] = None,
        uid_group_map: Optional[dict[str, int]] = None,
    ) -> None:
        self.feishu = feishu
        self.local = local
        self.group_notifiers = group_notifiers or {}
        self.uid_group_map = uid_group_map or {}
        self.last_errors: list[str] = []

    def _get_feishu_for_uid(self, uid: str) -> Optional[FeishuNotifier]:
        uid_str = normalize_bilibili_uid(uid)
        logger.debug(f"_get_feishu_for_uid: uid={uid_str}, uid_group_map_keys={list(self.uid_group_map.keys())}, group_notifiers_keys={list(self.group_notifiers.keys())}")
        if uid_str and uid_str != "None":
            # Try string key first, then int key for compatibility
            group_id = self.uid_group_map.get(uid_str)
            if group_id is None:
                try:
                    group_id = self.uid_group_map.get(int(uid_str))
                except (ValueError, TypeError):
                    pass
            if group_id is not None and group_id in self.group_notifiers:
                logger.debug(f"Found Feishu notifier for UID {uid_str} in group {group_id}")
                return self.group_notifiers[group_id]
            else:
                logger.debug(f"No group match for UID {uid_str}, group_id={group_id}")
        if self.feishu:
            logger.debug(f"Using default Feishu notifier for UID {uid_str}")
            return self.feishu
        logger.debug(f"No Feishu notifier available for UID {uid_str}")
        return None

    def has_any_feishu(self) -> bool:
        return bool(self.feishu or self.group_notifiers)

    def _record_feishu_image_errors(self, feishu: FeishuNotifier) -> None:
        for err in getattr(feishu, "last_image_upload_errors", []):
            if err not in self.last_errors:
                self.last_errors.append(err)

    def notify_new_video(self, video: dict[str, Any], author_name: str = "") -> bool:
        """Notify about a new video. Returns True if all enabled sinks succeeded."""
        feishu_ok = True
        local_ok = True

        uid = str(video.get("author_mid", ""))
        feishu = self._get_feishu_for_uid(uid)
        bvid = video.get("bvid", "")

        if feishu:
            logger.info(f"Notifying video {bvid} for UID {uid} via Feishu (group={self.uid_group_map.get(uid)})")
        elif self.has_any_feishu():
            msg = f"No Feishu notifier matched for UID {uid} (uid_group_map keys: {list(self.uid_group_map.keys())})"
            self.last_errors.append(msg)
            logger.warning(msg)
        else:
            msg = f"No Feishu configured at all, skipping notification for video {bvid}"
            self.last_errors.append(msg)
            logger.info(msg)

        if feishu:
            title = video.get("title", "新视频")
            link = video.get("link", "") or (f"https://www.bilibili.com/video/{bvid}/" if bvid else "")
            desc = (video.get("description") or "")[:500]
            play = video.get("play")
            pub_time = video.get("pub_time_utc", "")
            pic = video.get("pic", "")

            content_lines = [
                f"**UP主**: {author_name}",
            ]
            if bvid:
                content_lines.append(f"**BV号**: {bvid}")
            if pub_time:
                content_lines.append(f"**发布时间**: {pub_time}")
            if desc:
                content_lines.append(f"**简介**: {desc}")
            if play is not None:
                content_lines.append(f"**播放量**: {play}")

            image_urls: list[str] = []
            if pic:
                image_urls.append(pic)

            result = feishu.send_interactive_card(
                title=f"🎬 {title}",
                content_lines=content_lines,
                link=link,
                link_text="观看视频",
                header_color="blue",
                image_urls=image_urls,
            )
            self._record_feishu_image_errors(feishu)
            if result.get("code") == 0:
                logger.info(f"Feishu notification sent for video {bvid}")
            else:
                msg = f"Feishu notification failed for video {bvid}: {result}"
                self.last_errors.append(msg)
                logger.warning(msg)
                fallback_text = "\n".join([
                    f"新视频: {title}",
                    f"UP: {author_name}",
                    f"BV: {bvid}",
                    f"链接: {link}",
                ])
                fallback_result = feishu.send_text(fallback_text)
                if fallback_result.get("code") == 0:
                    logger.info(f"Feishu text fallback sent for video {bvid}")
                else:
                    fallback_msg = f"Feishu text fallback failed for video {bvid}: {fallback_result}"
                    self.last_errors.append(fallback_msg)
                    logger.warning(fallback_msg)
                    feishu_ok = False
        else:
            feishu_ok = not self.has_any_feishu()

        if self.local:
            filepath = self.local.write_update(
                uid=str(video.get("author_mid", "")),
                author_name=author_name,
                videos=[video],
                dynamics=[],
            )
            logger.info(f"Local notification written: {filepath}")

        return feishu_ok and local_ok

    def notify_new_dynamic(self, dynamic: dict[str, Any], author_name: str = "") -> bool:
        """Notify about a new dynamic post. Returns True if all enabled sinks succeeded."""
        feishu_ok = True
        local_ok = True

        uid = str(dynamic.get("author_mid", ""))
        feishu = self._get_feishu_for_uid(uid)
        dynamic_id = dynamic.get("dynamic_id", "")

        if feishu:
            logger.info(f"Notifying dynamic {dynamic_id} for UID {uid} via Feishu (group={self.uid_group_map.get(uid)})")
        elif self.has_any_feishu():
            msg = f"No Feishu notifier matched for UID {uid} (uid_group_map keys: {list(self.uid_group_map.keys())})"
            self.last_errors.append(msg)
            logger.warning(msg)
        else:
            msg = f"No Feishu configured at all, skipping notification for dynamic {dynamic_id}"
            self.last_errors.append(msg)
            logger.info(msg)

        if feishu:
            dtype = dynamic.get("type", "")
            title = dynamic.get("title", "")
            text = (dynamic.get("text") or "")[:1000]
            link = dynamic.get("link") or (f"https://www.bilibili.com/opus/{dynamic_id}" if dynamic_id else "")
            attached = dynamic.get("attached_video")
            images = dynamic.get("images", [])

            card_title = f"📢 {title}" if title else f"📢 {author_name}的新动态"

            content_lines = [
                f"**UP主**: {author_name}",
            ]

            dtype_label = {
                "DYNAMIC_TYPE_DRAW": "图文",
                "DYNAMIC_TYPE_AV": "视频",
                "DYNAMIC_TYPE_WORD": "文字",
                "DYNAMIC_TYPE_FORWARD": "转发",
                "DYNAMIC_TYPE_LIVE_RCMD": "直播",
            }.get(dtype, dtype)
            content_lines.append(f"**类型**: {dtype_label}")

            if text:
                escaped = text.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
                content_lines.append(f"**内容**:\n{escaped}")

            if attached:
                av_title = attached.get("title", "")
                av_link = attached.get("link", "")
                if av_title:
                    content_lines.append(f"**关联视频**: {av_title}")
                if av_link:
                    content_lines.append(f"[观看视频]({av_link})")

            image_urls: list[str] = []
            if attached and attached.get("pic"):
                image_urls.append(attached["pic"])
            image_urls.extend(images[:6])

            result = feishu.send_interactive_card(
                title=card_title,
                content_lines=content_lines,
                link=link or None,
                link_text="查看动态",
                header_color="green",
                image_urls=image_urls,
            )
            self._record_feishu_image_errors(feishu)
            if result.get("code") == 0:
                logger.info(f"Feishu notification sent for dynamic {dynamic_id}")
            else:
                msg = f"Feishu notification failed for dynamic {dynamic_id}: {result}"
                self.last_errors.append(msg)
                logger.warning(msg)
                fallback_text = "\n".join([
                    f"新动态: {title or author_name}",
                    f"UP: {author_name}",
                    f"类型: {dtype_label}",
                    f"内容: {text[:500] if text else '-'}",
                    f"链接: {link or '-'}",
                ])
                fallback_result = feishu.send_text(fallback_text)
                if fallback_result.get("code") == 0:
                    logger.info(f"Feishu text fallback sent for dynamic {dynamic_id}")
                else:
                    fallback_msg = f"Feishu text fallback failed for dynamic {dynamic_id}: {fallback_result}"
                    self.last_errors.append(fallback_msg)
                    logger.warning(fallback_msg)
                    feishu_ok = False
        else:
            feishu_ok = self.has_any_feishu() == False

        if self.local:
            uid = str(dynamic.get("author_mid", ""))
            filepath = self.local.write_update(
                uid=uid,
                author_name=author_name,
                videos=[],
                dynamics=[dynamic],
            )
            logger.info(f"Local notification written: {filepath}")

        return feishu_ok and local_ok

    def notify_check_summary(self, results: list[dict[str, Any]]) -> None:
        """Send a summary of a monitoring check cycle."""
        new_videos = sum(len(r.get("new_videos", [])) for r in results)
        new_dynamics = sum(len(r.get("new_dynamics", [])) for r in results)
        total_targets = len(results)

        if new_videos == 0 and new_dynamics == 0:
            logger.info("Check complete: no new updates detected")
            return

        # Feishu text summary
        if self.feishu:
            lines = [
                f"📊 B站监控检查完成",
                f"监控目标: {total_targets} 个UP主",
                f"新视频: {new_videos} 个",
                f"新动态: {new_dynamics} 个",
            ]
            for r in results:
                uid = r.get("uid", "")
                name = r.get("author_name", "")
                nv = len(r.get("new_videos", []))
                nd = len(r.get("new_dynamics", []))
                if nv > 0 or nd > 0:
                    lines.append(f"  - {name}(UID:{uid}): {nv}视频 {nd}动态")

            result = self.feishu.send_text("\n".join(lines))
            if result.get("code") == 0:
                logger.info("Feishu summary notification sent")
            else:
                logger.warning(f"Feishu summary failed: {result}")

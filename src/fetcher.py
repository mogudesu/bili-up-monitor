"""Bilibili data fetcher - fetches recent videos and dynamics.

Supports both cookie-less (public API) and cookie-authenticated access.
When cookies are provided, uses the full feed API for complete dynamic coverage.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Optional

_SUBPROCESS_KWARGS = {}
if sys.platform == "win32":
    _SUBPROCESS_KWARGS["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

logger = logging.getLogger("bilibili-monitor.fetcher")


def _ensure_ssl_certs():
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
        return
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        for name in ["cacert.pem", "certifi cacert.pem"]:
            candidate = os.path.join(base, name)
            if os.path.isfile(candidate):
                os.environ["SSL_CERT_FILE"] = candidate
                return
        if hasattr(sys, "_MEIPASS"):
            meipass = sys._MEIPASS
            for name in ["cacert.pem", "certifi cacert.pem"]:
                candidate = os.path.join(meipass, name)
                if os.path.isfile(candidate):
                    os.environ["SSL_CERT_FILE"] = candidate
                    return


_ensure_ssl_certs()

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
FEATURES = "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,forwardListHidden,decorationCard"
DYNAMIC_ID_EPOCH_OFFSET = 1498838400
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33,
    9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17,
    0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


# ---- Cookie Management ----

_global_cookie_dict: dict[str, str] = {}


def _pub_ts_from_dynamic_id(dynamic_id: Any) -> Optional[int]:
    """Decode a Bilibili dynamic/opus snowflake ID into a Unix timestamp."""
    try:
        raw = str(dynamic_id or "").split("?", 1)[0].strip()
        if not raw.isdigit():
            return None
        pub_ts = (int(raw) >> 32) + DYNAMIC_ID_EPOCH_OFFSET
        now = int(time.time())
        if 1_500_000_000 <= pub_ts <= now + 86400:
            return pub_ts
    except Exception:
        return None
    return None


def set_cookies(cookie_dict: dict[str, str]) -> None:
    """Set global cookies for authenticated API access."""
    global _global_cookie_dict
    _global_cookie_dict = {k: v for k, v in cookie_dict.items() if v}


def get_cookies() -> dict[str, str]:
    """Return current global cookie dict."""
    return dict(_global_cookie_dict)


def clear_cookies() -> None:
    """Clear all stored cookies."""
    global _global_cookie_dict
    _global_cookie_dict = {}


def parse_cookie_string(raw: str) -> dict[str, str]:
    """Parse a cookie string in various formats into a dict.

    Supported formats:
    - Browser cookie header: "key1=value1; key2=value2"
    - Line-separated: "key1=value1\\nkey2=value2"
    - JSON: {"key1": "value1", "key2": "value2"}
    - Mixed with spaces/newlines
    """
    raw = raw.strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items() if v}
    except (json.JSONDecodeError, ValueError):
        pass

    result: dict[str, str] = {}
    for line in raw.replace(";", "\n").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and value:
            result[key] = value

    return result


def check_cookie_status() -> dict[str, Any]:
    """Check the validity of stored cookies by calling the nav API.

    Returns a dict with status info:
    - has_sessdata: bool
    - is_logged_in: bool
    - username: str or None
    - vip_type: int or None
    - message: str
    """
    sessdata = _global_cookie_dict.get("SESSDATA", "")
    if not sessdata:
        return {
            "has_sessdata": False,
            "is_logged_in": False,
            "username": None,
            "vip_type": None,
            "message": "未设置 Cookie（使用公开 API）",
        }

    try:
        nav = _http_get_json("https://api.bilibili.com/x/web-interface/nav")
        code = nav.get("code", -1)
        data = nav.get("data", {})

        if code == 0 and data.get("isLogin"):
            return {
                "has_sessdata": True,
                "is_logged_in": True,
                "username": data.get("uname"),
                "vip_type": data.get("vipType"),
                "mid": data.get("mid"),
                "message": f"已登录: {data.get('uname', '未知')}",
            }
        elif code == -101:
            return {
                "has_sessdata": True,
                "is_logged_in": False,
                "username": None,
                "vip_type": None,
                "message": "Cookie 已过期或无效",
            }
        else:
            msg = nav.get("message", "未知错误")
            return {
                "has_sessdata": True,
                "is_logged_in": False,
                "username": None,
                "vip_type": None,
                "message": f"验证失败: {msg}",
            }
    except Exception as exc:
        return {
            "has_sessdata": True,
            "is_logged_in": False,
            "username": None,
            "vip_type": None,
            "message": f"验证请求失败: {exc}",
        }


def _build_cookie_header() -> str:
    """Build a Cookie header string from global cookies."""
    if not _global_cookie_dict:
        return ""
    return "; ".join(f"{k}={v}" for k, v in _global_cookie_dict.items())


def qrcode_generate() -> dict[str, Any]:
    url = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc) or "SSL" in str(exc):
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
            else:
                raise
    except Exception as exc:
        raise RuntimeError(f"QR generate request failed: {exc}") from exc

    payload = json.loads(body)
    if payload.get("code") != 0:
        msg = payload.get("message", "unknown error")
        raise RuntimeError(f"B站二维码生成失败: {msg} (code={payload.get('code')})")
    data = payload.get("data", {})
    qrcode_key = data.get("qrcode_key", "")
    qr_url = data.get("url", "")
    if not qrcode_key or not qr_url:
        raise RuntimeError(f"B站二维码返回数据不完整: qrcode_key={qrcode_key}, url={qr_url}")
    return {
        "qrcode_key": qrcode_key,
        "url": qr_url,
    }


def qrcode_poll(qrcode_key: str) -> dict[str, Any]:
    """Poll QR code scan status.

    Returns dict with:
    - code: int - status code (0=success, 86038=expired, 86090=scanned, 86101=waiting)
    - message: str
    - cookies: dict - cookie key-value pairs if login successful
    - refresh_token: str or None
    """
    poll_url = f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qrcode_key}"
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
    }
    req = urllib.request.Request(poll_url, headers=headers)
    try:
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                resp_headers = resp.headers
        except urllib.error.URLError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc) or "SSL" in str(exc):
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    resp_headers = resp.headers
            else:
                raise
    except Exception as exc:
        raise RuntimeError(f"QR poll request failed: {exc}") from exc

    payload = json.loads(body)
    data = payload.get("data", {})
    code = data.get("code", -1)
    message = data.get("message", "")

    result = {
        "code": code,
        "message": message,
        "cookies": {},
        "refresh_token": data.get("refresh_token"),
    }

    if code == 0:
        cookie_dict: dict[str, str] = {}
        set_cookie_headers = resp_headers.get_all("Set-Cookie") or []
        for header_val in set_cookie_headers:
            parts = header_val.split(";")[0].strip()
            if "=" in parts:
                k, _, v = parts.partition("=")
                cookie_dict[k.strip()] = v.strip()
        result["cookies"] = cookie_dict
        if cookie_dict:
            set_cookies(cookie_dict)

    return result


# ---- HTTP Helpers ----

def _build_url(url: str, params: dict[str, Any] | None = None) -> str:
    if not params:
        return url
    return f"{url}?{urllib.parse.urlencode(params)}"


def _http_get_text(url: str, params: dict[str, Any] | None = None, timeout: int = 20) -> str:
    full_url = _build_url(url, params)
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://space.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
    }
    cookie_header = _build_cookie_header()
    if cookie_header:
        headers["Cookie"] = cookie_header
    req = urllib.request.Request(full_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc) or "SSL" in str(exc):
            logger.warning(f"SSL verify failed for {full_url}, retrying without verification")
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Request failed for {full_url}: {exc}") from exc
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {full_url}: {body[:240]}") from exc


def _http_get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    text = _http_get_text(url, params)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from {_build_url(url, params)}: {text[:240]}") from exc


def _fetch_opus_web_detail(opus_id: str) -> Optional[dict[str, Any]]:
    """Fetch opus page initial state when the detail API is rate-limited."""
    if not opus_id:
        return None
    html = _http_get_text(f"https://www.bilibili.com/opus/{opus_id}", timeout=20)
    match = re.search(r"<script>window\.__INITIAL_STATE__=(.*?);\(function\(\)", html, re.S)
    if not match:
        return None
    try:
        state = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    detail = state.get("detail")
    if isinstance(detail, dict):
        return detail
    return None


def _extract_opus_id_from_attrs(attrs: dict[str, str]) -> str:
    for key in ("data-url", "href", "src", "data-dyn-id", "data-id"):
        value = attrs.get(key) or ""
        match = re.search(r"(?:/opus/|^)(\d{12,})", value)
        if match:
            return match.group(1)
    return ""


def _normalize_html_text(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\u3000]+", " ", line).strip() for line in value.split("\n")]
    cleaned: list[str] = []
    blank = False
    for line in lines:
        if line:
            cleaned.append(line)
            blank = False
        elif cleaned and not blank:
            cleaned.append("")
            blank = True
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    return "\n".join(cleaned).strip()


class _OpusCardHTMLParser(HTMLParser):
    """Extract opus title/body from a space dynamic card without opening opus pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: dict[str, dict[str, str]] = {}
        self.current_opus_id = ""
        self.capture_title_for = ""
        self.capture_body_for = ""
        self.title_depth = 0
        self.body_depth = 0
        self.title_chunks: list[str] = []
        self.body_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, Optional[str]]]) -> None:
        attrs = {key: value or "" for key, value in attrs_list}
        opus_id = _extract_opus_id_from_attrs(attrs)
        if opus_id:
            self.current_opus_id = opus_id
            self.cards.setdefault(opus_id, {})

        class_name = attrs.get("class", "")
        if tag == "div" and "dyn-card-opus__title" in class_name:
            target_id = opus_id or self.current_opus_id
            if target_id:
                self.capture_title_for = target_id
                self.title_depth = 1
                self.title_chunks = []
                self.cards.setdefault(target_id, {})
            return

        if self.capture_title_for:
            self.title_depth += 1

        if tag == "p" and "bili-ellipsis" in class_name and self.current_opus_id:
            self.capture_body_for = self.current_opus_id
            self.body_depth = 1
            self.body_chunks = []
            self.cards.setdefault(self.current_opus_id, {})
            return

        if self.capture_body_for:
            if tag == "img":
                alt = attrs.get("alt", "")
                if alt:
                    self.body_chunks.append(alt)
                return
            self.body_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self.capture_title_for:
            self.title_depth -= 1
            if self.title_depth <= 0:
                title = _normalize_html_text("".join(self.title_chunks))
                if title:
                    self.cards.setdefault(self.capture_title_for, {})["title"] = title
                self.capture_title_for = ""
                self.title_chunks = []
                self.title_depth = 0
                return

        if self.capture_body_for:
            self.body_depth -= 1
            if self.body_depth <= 0:
                text = _normalize_html_text("".join(self.body_chunks))
                if text:
                    self.cards.setdefault(self.capture_body_for, {})["text"] = text
                self.capture_body_for = ""
                self.body_chunks = []
                self.body_depth = 0

    def handle_data(self, data: str) -> None:
        if self.capture_title_for:
            self.title_chunks.append(data)
        elif self.capture_body_for:
            self.body_chunks.append(data)


def _extract_opus_cards_from_html(page_html: str) -> dict[str, dict[str, str]]:
    parser = _OpusCardHTMLParser()
    parser.feed(page_html or "")
    parser.close()
    return {
        opus_id: card
        for opus_id, card in parser.cards.items()
        if card.get("title") or card.get("text")
    }


def _fetch_space_dynamic_html_opus_cards(uid: str) -> dict[str, dict[str, str]]:
    """Fetch space dynamic HTML and parse card title/body from the list page."""
    if not uid:
        return {}
    try:
        page_html = _http_get_text(f"https://space.bilibili.com/{uid}/dynamic", timeout=20)
    except Exception as exc:
        logger.debug(f"Space dynamic HTML fallback failed for uid {uid}: {exc}")
        return {}
    return _extract_opus_cards_from_html(page_html)


# ---- WBI Signing ----

def _get_mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB if i < len(orig))[:32]


def _fetch_wbi_keys() -> tuple[str, str]:
    nav = _http_get_json("https://api.bilibili.com/x/web-interface/nav")
    data = nav.get("data", {})
    wbi = data.get("wbi_img", {})
    img_url = wbi.get("img_url", "")
    sub_url = wbi.get("sub_url", "")
    if not img_url or not sub_url:
        raise RuntimeError("Failed to fetch WBI keys from x/web-interface/nav")
    img_key = img_url.rsplit("/", 1)[1].split(".", 1)[0]
    sub_key = sub_url.rsplit("/", 1)[1].split(".", 1)[0]
    return img_key, sub_key


def _sign_wbi_params(params: dict[str, Any], img_key: str, sub_key: str) -> dict[str, str]:
    mixin_key = _get_mixin_key(img_key + sub_key)
    signed = dict(params)
    signed["wts"] = round(time.time())
    signed = dict(sorted(signed.items()))
    filtered = {
        key: re.sub(r"[!'()*]", "", str(value))
        for key, value in signed.items()
    }
    query = urllib.parse.urlencode(filtered)
    filtered["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return filtered


# ---- yt-dlp ----

def _resolve_yt_dlp_command() -> Optional[list[str]]:
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    if shutil.which("uvx"):
        return ["uvx", "--from", "yt-dlp", "yt-dlp"]
    if shutil.which("uv"):
        return ["uv", "tool", "run", "--from", "yt-dlp", "yt-dlp"]
    # Try Python module path
    py_exe = shutil.which("py") or shutil.which("python3") or shutil.which("python")
    if py_exe:
        try:
            result = subprocess.run(
                [py_exe, "-c", "import yt_dlp"],
                capture_output=True, timeout=5, check=False,
                **_SUBPROCESS_KWARGS,
            )
            if result.returncode == 0:
                return [py_exe, "-m", "yt_dlp"]
        except Exception:
            pass
    return None


def _run_json_command(command: list[str]) -> Any:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
        **_SUBPROCESS_KWARGS,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"Command failed ({completed.returncode}): {' '.join(command)} :: {stderr[:240]}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Command returned invalid JSON: {' '.join(command)}") from exc


def _should_retry_transient(error_text: str) -> bool:
    lowered = error_text.lower()
    return " 412" in lowered or " 352" in lowered or "request is blocked" in lowered


def _list_recent_bvids_via_yt_dlp(uid: str, limit: int) -> tuple[list[str], str]:
    command_prefix = _resolve_yt_dlp_command()
    if not command_prefix:
        raise RuntimeError("yt-dlp or uv/uvx is not available")

    command = command_prefix + [
        "--flat-playlist",
        "--playlist-end", str(limit),
        "--dump-single-json",
        f"https://space.bilibili.com/{uid}/video",
    ]
    last_error: Optional[RuntimeError] = None
    for attempt in range(2):
        try:
            payload = _run_json_command(command)
            break
        except RuntimeError as exc:
            last_error = exc
            if attempt == 0 and _should_retry_transient(str(exc)):
                time.sleep(3)
                continue
            raise
    else:
        raise last_error or RuntimeError("yt-dlp failed without an error object")

    entries = payload.get("entries") or []
    bvids: list[str] = []
    for entry in entries:
        bvid = entry.get("id")
        if isinstance(bvid, str) and bvid.startswith("BV"):
            bvids.append(bvid)
    return bvids, "yt_dlp_space_playlist"


# ---- Video Details ----

def _canonical_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    if url.startswith("//"):
        return f"https:{url}"
    return url


def _iso_utc(ts: Optional[int | float | str]) -> Optional[str]:
    if not ts:
        return None
    try:
        ts = int(ts)
    except (ValueError, TypeError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def fetch_video_detail(bvid: str) -> dict[str, Any]:
    """Fetch video metadata by BVID from public API."""
    payload = _http_get_json("https://api.bilibili.com/x/web-interface/view", {"bvid": bvid})
    if payload.get("code") != 0:
        raise RuntimeError(f"x/web-interface/view returned code={payload.get('code')} for {bvid}")
    data = payload.get("data") or {}
    owner = data.get("owner") or {}
    stat = data.get("stat") or {}
    pub_ts = int(data.get("pubdate") or data.get("ctime") or 0)
    pic = data.get("pic", "")
    return {
        "title": data.get("title"),
        "bvid": data.get("bvid") or bvid,
        "link": f"https://www.bilibili.com/video/{data.get('bvid') or bvid}/",
        "description": data.get("desc"),
        "pub_ts": pub_ts,
        "pub_time_utc": _iso_utc(pub_ts),
        "length": data.get("duration"),
        "pic": _canonical_url(pic),
        "play": stat.get("view"),
        "comment": stat.get("reply"),
        "like": stat.get("like"),
        "author_name": owner.get("name"),
        "author_mid": owner.get("mid"),
        "source": "public_view_api",
    }


def fetch_user_info(uid: str) -> dict[str, Any]:
    """Fetch basic user info by UID. Uses WBI signing for better reliability."""
    # Try WBI-signed request first
    try:
        img_key, sub_key = _fetch_wbi_keys()
        params = _sign_wbi_params({"mid": uid}, img_key, sub_key)
        payload = _http_get_json(
            "https://api.bilibili.com/x/space/wbi/acc/info",
            params,
        )
        if payload.get("code") == 0:
            data = payload.get("data") or {}
            return {
                "uid": uid,
                "name": data.get("name"),
                "sign": data.get("sign"),
                "face": _canonical_url(data.get("face")),
                "space_url": f"https://space.bilibili.com/{uid}",
                "dynamic_url": f"https://space.bilibili.com/{uid}/dynamic",
            }
    except Exception:
        pass

    # Fallback: try card API which is less restricted
    try:
        payload = _http_get_json(
            "https://api.bilibili.com/x/space/acc/info",
            {"mid": uid},
        )
        if payload.get("code") == 0:
            data = payload.get("data") or {}
            return {
                "uid": uid,
                "name": data.get("name"),
                "sign": data.get("sign"),
                "face": _canonical_url(data.get("face")),
                "space_url": f"https://space.bilibili.com/{uid}",
                "dynamic_url": f"https://space.bilibili.com/{uid}/dynamic",
            }
    except Exception:
        pass

    # Final fallback: try card API
    try:
        payload = _http_get_json(
            "https://api.bilibili.com/x/web-interface/card",
            {"mid": uid, "photo": "true"},
        )
        if payload.get("code") == 0:
            card = (payload.get("data") or {}).get("card") or {}
            return {
                "uid": uid,
                "name": card.get("name"),
                "sign": card.get("sign"),
                "face": _canonical_url(card.get("face")),
                "space_url": f"https://space.bilibili.com/{uid}",
                "dynamic_url": f"https://space.bilibili.com/{uid}/dynamic",
            }
    except Exception:
        pass

    # Return minimal info if all APIs fail
    return {
        "uid": uid,
        "name": f"UID:{uid}",
        "sign": "",
        "face": None,
        "space_url": f"https://space.bilibili.com/{uid}",
        "dynamic_url": f"https://space.bilibili.com/{uid}/dynamic",
    }


# ---- Recent Videos ----

def fetch_recent_videos_via_yt_dlp(
    uid: str, since_ts: int, limit: int = 8
) -> tuple[list[dict[str, Any]], list[str], str]:
    """Fetch recent videos using yt-dlp + public view API."""
    warnings: list[str] = []
    bvids, source = _list_recent_bvids_via_yt_dlp(uid, limit)
    items: list[dict[str, Any]] = []
    for bvid in bvids:
        try:
            video = fetch_video_detail(bvid)
        except Exception as exc:
            warnings.append(f"view detail failed for {bvid}: {exc}")
            continue
        pub_ts = int(video.get("pub_ts") or 0)
        if pub_ts and pub_ts < since_ts:
            continue
        items.append(video)
    return items, warnings, source


def fetch_recent_videos_via_public_api(
    uid: str,
    since_ts: int,
    max_pages: int = 2,
    page_size: int = 10,
) -> tuple[list[dict[str, Any]], str]:
    """Fetch recent videos via public space/arc/search API (fallback)."""
    items: list[dict[str, Any]] = []

    img_key, sub_key = "", ""
    try:
        img_key, sub_key = _fetch_wbi_keys()
    except Exception:
        pass

    for page in range(1, max_pages + 1):
        base_params: dict[str, Any] = {"mid": uid, "pn": page, "ps": page_size, "order": "pubdate"}
        if img_key and sub_key:
            params = _sign_wbi_params(base_params, img_key, sub_key)
            url = "https://api.bilibili.com/x/space/wbi/arc/search"
        else:
            params = base_params
            url = "https://api.bilibili.com/x/space/arc/search"
        payload = _http_get_json(url, params)
        if payload.get("code") != 0:
            raise RuntimeError(
                f"{url} returned code={payload.get('code')} message={payload.get('message')}"
            )
        vlist = (((payload.get("data") or {}).get("list") or {}).get("vlist") or [])
        if not vlist:
            break
        for video in vlist:
            created = int(video.get("created") or 0)
            if created < since_ts:
                continue
            items.append({
                "title": video.get("title"),
                "bvid": video.get("bvid"),
                "link": f"https://www.bilibili.com/video/{video.get('bvid')}/" if video.get("bvid") else None,
                "description": video.get("description"),
                "pub_ts": created,
                "pub_time_utc": _iso_utc(created),
                "length": video.get("length"),
                "play": video.get("play"),
                "comment": video.get("comment"),
                "author_name": video.get("author"),
                "author_mid": video.get("mid"),
                "source": "space_arc_search",
            })
    return items, "space_arc_search"


def fetch_recent_videos_via_search(
    uid: str,
    author_name: str,
    since_ts: int,
    img_key: str,
    sub_key: str,
    max_pages: int = 2,
    page_size: int = 10,
) -> tuple[list[dict[str, Any]], list[str], str]:
    """Fetch recent videos by searching for the UP name. Best-effort fallback."""
    import requests as _requests

    warnings: list[str] = []
    items: list[dict[str, Any]] = []
    seen_bvids: set[str] = set()

    session = _requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
    })
    cookie_header = _build_cookie_header()
    if cookie_header:
        for pair in cookie_header.split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                session.cookies.set(k.strip(), v.strip())

    for page in range(1, max_pages + 1):
        params = _sign_wbi_params(
            {
                "keyword": author_name,
                "search_type": "video",
                "page": page,
                "page_size": page_size,
                "order": "pubdate",
            },
            img_key, sub_key,
        )
        try:
            r = session.get(
                "https://api.bilibili.com/x/web-interface/search/type",
                params=params,
                timeout=15,
            )
            payload = r.json()
        except Exception as exc:
            warnings.append(f"search API page {page} failed: {exc}")
            break

        if payload.get("code") != 0:
            warnings.append(f"search API returned code={payload.get('code')}")
            break

        results = ((payload.get("data") or {}).get("result") or [])
        if not results:
            break

        for r in results:
            bvid = r.get("bvid", "")
            mid = r.get("mid", "")
            author = r.get("author", "")
            if mid and str(mid) != str(uid):
                continue
            if not mid and author != author_name:
                continue

            if not bvid or bvid in seen_bvids:
                continue
            seen_bvids.add(bvid)

            title = re.sub(r"<[^>]+>", "", r.get("title", ""))
            pub_ts = int(r.get("pubdate") or 0)
            if pub_ts and pub_ts < since_ts:
                continue

            try:
                video = fetch_video_detail(bvid)
                items.append(video)
            except Exception as exc:
                warnings.append(f"view detail failed for search result {bvid}: {exc}")
                items.append({
                    "title": title,
                    "bvid": bvid,
                    "link": f"https://www.bilibili.com/video/{bvid}/",
                    "description": re.sub(r"<[^>]+>", "", r.get("description", "")),
                    "pub_ts": pub_ts,
                    "pub_time_utc": _iso_utc(pub_ts),
                    "author_name": author,
                    "author_mid": uid,
                    "source": "search_api",
                })

    return items, warnings, "search_api"


def fetch_recent_videos(
    uid: str,
    since_ts: int,
    max_pages: int = 2,
    page_size: int = 10,
    author_name: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch recent videos with multiple fallback strategies."""
    warnings: list[str] = []
    limit = min(max_pages * page_size, 8)

    # Strategy 1: search by UP name (most reliable against risk control)
    if author_name:
        try:
            img_key, sub_key = _fetch_wbi_keys()
            items, search_warnings, source = fetch_recent_videos_via_search(
                uid, author_name, since_ts, img_key, sub_key, max_pages, page_size,
            )
            warnings.extend(search_warnings)
            if items:
                return items, {"status": "ok", "source": source, "warnings": warnings}
        except Exception as exc:
            warnings.append(f"search API failed: {exc}")

    # Strategy 2: yt-dlp
    try:
        items, local_warnings, source = fetch_recent_videos_via_yt_dlp(uid, since_ts, limit)
        return items, {"status": "ok", "source": source, "warnings": warnings + local_warnings}
    except Exception as exc:
        warnings.append(f"yt-dlp playlist path failed: {exc}")

    # Strategy 3: public space/arc/search API
    try:
        items, source = fetch_recent_videos_via_public_api(uid, since_ts, max_pages, page_size)
        return items, {"status": "ok", "source": source, "warnings": warnings}
    except Exception as exc:
        if _should_retry_transient(str(exc)):
            time.sleep(2)
            try:
                items, source = fetch_recent_videos_via_public_api(uid, since_ts, max_pages, page_size)
                return items, {"status": "ok", "source": source, "warnings": warnings}
            except Exception as retry_exc:
                warnings.append(f"public video API retry failed: {retry_exc}")
        warnings.append(f"public video API fallback failed: {exc}")

    return [], {"status": "unverified-no-cookie", "source": None, "warnings": warnings}


# ---- Dynamics ----

def _module_map(modules: Any) -> dict[str, Any]:
    """Normalize Bilibili dynamic modules from dict or desktop list shape."""
    if isinstance(modules, dict):
        return modules
    result: dict[str, Any] = {}
    if isinstance(modules, list):
        for module in modules:
            if not isinstance(module, dict):
                continue
            if "module_author" in module:
                result["module_author"] = module.get("module_author") or {}
            if "module_dynamic" in module:
                module_dynamic = module.get("module_dynamic") or {}
                existing_dynamic = result.get("module_dynamic")
                if isinstance(module_dynamic, dict) and isinstance(existing_dynamic, dict):
                    existing_desc = existing_dynamic.get("desc")
                    dynamic_desc = module_dynamic.get("desc")
                    result["module_dynamic"] = module_dynamic
                    if existing_desc and not dynamic_desc:
                        result["module_dynamic"]["desc"] = existing_desc
                    elif isinstance(existing_desc, dict) and isinstance(dynamic_desc, dict):
                        result["module_dynamic"]["desc"] = {**existing_desc, **dynamic_desc}
                else:
                    result["module_dynamic"] = module_dynamic
            if "module_title" in module:
                result["module_title"] = module.get("module_title") or {}
            if "module_content" in module:
                result["module_content"] = module.get("module_content") or {}
            if "module_top" in module:
                result["module_top"] = module.get("module_top") or {}
            if "module_desc" in module:
                # Merge module_desc into module_dynamic.desc if module_dynamic exists,
                # otherwise create module_dynamic with desc
                raw_module_desc = module.get("module_desc") or {}
                # Normalize module_desc to dict (it could be a string in some API versions)
                if isinstance(raw_module_desc, str):
                    module_desc: dict[str, Any] = {"text": raw_module_desc}
                elif isinstance(raw_module_desc, dict):
                    module_desc = raw_module_desc
                else:
                    module_desc = {}
                result["module_desc"] = module_desc
                if "module_dynamic" not in result:
                    result["module_dynamic"] = {"desc": module_desc}
                else:
                    existing_dynamic = result["module_dynamic"]
                    if isinstance(existing_dynamic, dict):
                        existing_desc_raw = existing_dynamic.get("desc")
                        if isinstance(existing_desc_raw, str):
                            existing_desc: dict[str, Any] = {"text": existing_desc_raw}
                        elif isinstance(existing_desc_raw, dict):
                            existing_desc = existing_desc_raw
                        else:
                            existing_desc = {}
                        # Merge: module_desc takes precedence, but only for non-empty values
                        merged_desc = {**existing_desc}
                        for key, value in module_desc.items():
                            if value or key not in merged_desc:
                                merged_desc[key] = value
                        result["module_dynamic"]["desc"] = merged_desc
                    else:
                        result["module_dynamic"] = {"desc": module_desc}
            if "module_stat" in module:
                result["module_stat"] = module.get("module_stat") or {}
    return result


def _extract_dynamic_pub_ts(item: dict[str, Any]) -> Optional[int]:
    """Read a publish timestamp from either dynamic-detail or raw opus shape."""
    modules = _module_map(item.get("modules") or {})
    author = modules.get("module_author") or {}
    for value in (
        author.get("pub_ts"),
        item.get("pub_ts"),
        item.get("ctime"),
        item.get("pub_time"),
        item.get("_estimated_pub_ts"),
    ):
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return _pub_ts_from_dynamic_id(item.get("id_str") or item.get("opus_id"))


def _backfill_ordered_opus_pub_ts(items: list[dict[str, Any]], since_ts: int) -> None:
    """Backfill raw opus timestamps using feed order when the lower bound is recent.

    The opus feed is returned newest first. When detail requests are rate-limited,
    raw image/text posts may lack pub_ts. If a later item in the same ordered feed
    has a known timestamp inside the trailing window, every raw item before it is
    newer than that lower-bound timestamp and can safely be included.
    """
    lower_bound: Optional[int] = None
    for item in reversed(items):
        known_ts = _extract_dynamic_pub_ts(item)
        if known_ts is not None and not item.get("_estimated_pub_ts"):
            lower_bound = known_ts
            continue
        if item.get("opus_id") and lower_bound is not None and lower_bound >= since_ts:
            item["_estimated_pub_ts"] = lower_bound + 1
            item["_estimated_pub_ts_reason"] = "feed_order_lower_bound"


def _extract_archive_from_dynamic(item: dict[str, Any]) -> dict[str, Any]:
    modules = _module_map(item.get("modules") or {})
    dynamic = modules.get("module_dynamic") or {}
    major = dynamic.get("major") or {}
    archive = (major.get("archive") if isinstance(major, dict) else None) or {}
    if archive:
        return archive

    orig = item.get("orig") or {}
    orig_modules = _module_map(orig.get("modules") or {})
    orig_dynamic = orig_modules.get("module_dynamic") or {}
    orig_major = orig_dynamic.get("major") or {}
    return (orig_major.get("archive") if isinstance(orig_major, dict) else None) or {}


def _extract_module_content_text(module_content: Any) -> str:
    if not isinstance(module_content, dict):
        return ""
    chunks: list[str] = []
    for paragraph in module_content.get("paragraphs") or []:
        if not isinstance(paragraph, dict):
            continue
        text_block = paragraph.get("text") or {}
        if not isinstance(text_block, dict):
            continue
        for node in text_block.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            word = node.get("word")
            if isinstance(word, dict):
                chunks.append(str(word.get("words") or ""))
                continue
            rich = node.get("rich")
            if isinstance(rich, dict):
                chunks.append(str(rich.get("text") or rich.get("orig_text") or ""))
    return "".join(chunks).strip()


def _parse_dynamic_item(item: dict[str, Any], source: str) -> dict[str, Any]:
    modules = _module_map(item.get("modules") or {})
    author = modules.get("module_author") or {}
    dynamic = modules.get("module_dynamic") or {}
    module_title = modules.get("module_title") or {}
    module_content = modules.get("module_content") or {}
    module_top = modules.get("module_top") or {}
    raw_desc = dynamic.get("desc")
    if isinstance(raw_desc, str):
        desc: dict[str, Any] = {"text": raw_desc}
    elif isinstance(raw_desc, dict):
        desc = raw_desc
    else:
        desc = {}

    orig = item.get("orig") or {}
    orig_modules = _module_map(orig.get("modules") or {})
    orig_dynamic = orig_modules.get("module_dynamic") or {}
    raw_orig_desc = orig_dynamic.get("desc")
    if isinstance(raw_orig_desc, str):
        orig_desc: dict[str, Any] = {"text": raw_orig_desc}
    elif isinstance(raw_orig_desc, dict):
        orig_desc = raw_orig_desc
    else:
        orig_desc = {}

    archive = _extract_archive_from_dynamic(item)
    attached_video = None
    if archive:
        av_bvid = archive.get("bvid", "")
        attached_video = {
            "title": archive.get("title"),
            "bvid": av_bvid,
            "link": _canonical_url(archive.get("jump_url")) or (f"https://www.bilibili.com/video/{av_bvid}/" if av_bvid else None),
            "description": archive.get("desc"),
            "pic": _canonical_url(archive.get("cover")),
        }

    user = author.get("user") if isinstance(author.get("user"), dict) else {}

    title = (item.get("_html_title") or "").strip()
    if not title and isinstance(module_title, dict):
        title = (module_title.get("text") or "").strip()
    major = dynamic.get("major") or {}
    if isinstance(major, dict):
        if not title and major.get("opus"):
            title = (major["opus"].get("title") or "").strip()
        if not title and major.get("archive"):
            title = (major["archive"].get("title") or "").strip()

    if not title and attached_video and attached_video.get("title"):
        title = attached_video["title"]

    images: list[str] = []
    if isinstance(module_top, dict):
        display = module_top.get("display") or {}
        if isinstance(display, dict):
            album = display.get("album") or {}
            if isinstance(album, dict):
                for pic_obj in album.get("pics") or []:
                    if isinstance(pic_obj, dict):
                        url = pic_obj.get("url") or pic_obj.get("src") or ""
                        if url:
                            images.append(_canonical_url(url) or url)
    if isinstance(major, dict):
        if major.get("opus"):
            for pic_obj in (major["opus"].get("pics") or []):
                url = pic_obj.get("url") or pic_obj.get("src") or ""
                if url:
                    images.append(_canonical_url(url) or url)
        if major.get("draw"):
            draw_items = major["draw"]
            if isinstance(draw_items, dict):
                for img_item in draw_items.get("items") or []:
                    src = img_item.get("src") or img_item.get("url") or ""
                    if src:
                        images.append(_canonical_url(src) or src)
            elif isinstance(draw_items, list):
                # Some API versions return draw as a direct list of images
                for img_item in draw_items:
                    if isinstance(img_item, dict):
                        src = img_item.get("src") or img_item.get("url") or ""
                        if src:
                            images.append(_canonical_url(src) or src)
                    elif isinstance(img_item, str):
                        images.append(_canonical_url(img_item) or img_item)
        if major.get("archive"):
            cover = _canonical_url(major["archive"].get("cover"))
            if cover:
                images.append(cover)
    dyn_draw = dynamic.get("dyn_draw") if isinstance(dynamic, dict) else None
    if isinstance(dyn_draw, dict):
        for img_item in dyn_draw.get("items") or []:
            if isinstance(img_item, dict):
                src = img_item.get("src") or img_item.get("url") or ""
                if src:
                    images.append(_canonical_url(src) or src)
    if not images:
        for pic_obj in (desc.get("images") or []):
            if isinstance(pic_obj, dict):
                src = pic_obj.get("src") or pic_obj.get("url") or pic_obj.get("img_src") or ""
            elif isinstance(pic_obj, str):
                src = pic_obj
            else:
                src = ""
            if src:
                images.append(_canonical_url(src) or src)
    if not images and orig:
        orig_major = orig_dynamic.get("major") or {}
        if isinstance(orig_major, dict):
            if orig_major.get("opus"):
                for pic_obj in (orig_major["opus"].get("pics") or []):
                    url = pic_obj.get("url") or pic_obj.get("src") or ""
                    if url:
                        images.append(_canonical_url(url) or url)
            if orig_major.get("draw"):
                orig_draw = orig_major["draw"]
                if isinstance(orig_draw, dict):
                    for img_item in orig_draw.get("items") or []:
                        src = img_item.get("src") or img_item.get("url") or ""
                        if src:
                            images.append(_canonical_url(src) or src)
                elif isinstance(orig_draw, list):
                    for img_item in orig_draw:
                        if isinstance(img_item, dict):
                            src = img_item.get("src") or img_item.get("url") or ""
                            if src:
                                images.append(_canonical_url(src) or src)
                        elif isinstance(img_item, str):
                            images.append(_canonical_url(img_item) or img_item)
            if orig_major.get("archive"):
                cover = _canonical_url(orig_major["archive"].get("cover"))
                if cover:
                    images.append(cover)

    text = (item.get("_html_content") or "").strip()
    if not text:
        text = _extract_module_content_text(module_content)
    if not text:
        text = desc.get("text") or orig_desc.get("text") or ""
    if not text and desc.get("rich_text_nodes"):
        text = "".join(
            node.get("text") or node.get("orig_text") or "" for node in desc["rich_text_nodes"]
        )
    if not text:
        text = item.get("_opus_content", "")
    if not text and isinstance(major, dict) and major.get("opus"):
        raw_summary = major["opus"].get("summary")
        if isinstance(raw_summary, str):
            text = raw_summary
        elif isinstance(raw_summary, dict):
            text = raw_summary.get("text", "")
    raw_opus_content = (item.get("_opus_content") or "").strip()
    if not title and raw_opus_content and text and raw_opus_content not in text:
        title = raw_opus_content

    dynamic_id = item.get("id_str") or ""
    dynamic_link = f"https://www.bilibili.com/opus/{dynamic_id}" if dynamic_id else None
    pub_ts = author.get("pub_ts") or _pub_ts_from_dynamic_id(dynamic_id)

    return {
        "dynamic_id": dynamic_id,
        "type": item.get("type") or ("DYNAMIC_TYPE_DRAW" if images else ""),
        "title": title,
        "author_name": author.get("name") or user.get("name"),
        "author_mid": author.get("mid") or user.get("mid"),
        "pub_ts": pub_ts,
        "pub_time": author.get("pub_time"),
        "pub_time_utc": _iso_utc(pub_ts),
        "link": dynamic_link,
        "text": text,
        "attached_video": attached_video,
        "images": images,
        "source": source,
    }


def fetch_dynamic_feed_with_cookie(
    uid: str, since_ts: int = 0, max_pages: int = 3,
) -> list[dict[str, Any]]:
    """Fetch dynamics using cookie-authenticated feed API.

    Returns full dynamic items with modules structure (including FORWARD type).
    Requires SESSDATA cookie to be set via set_cookies().
    """
    all_items: list[dict[str, Any]] = []
    offset = ""

    for _ in range(max_pages):
        params: dict[str, Any] = {
            "host_mid": uid, "offset": offset,
            "timezone_offset": -480, "features": FEATURES,
        }
        payload = _http_get_json(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
            params,
        )
        if payload.get("code") != 0:
            logger.debug(f"Cookie feed API returned code={payload.get('code')}")
            break

        data = payload.get("data") or {}
        items = data.get("items") or []
        all_items.extend(items)

        if not data.get("has_more"):
            break
        offset = data.get("offset") or ""
        if not offset:
            break

    return all_items


def fetch_dynamic_feed_probe(
    uid: str, img_key: str, sub_key: str, since_ts: int = 0, max_pages: int = 5,
) -> list[dict[str, Any]]:
    """Best-effort dynamic feed probe without cookies.

    Uses opus feed API to get a list of dynamics. Tries to fetch detail for
    each to get timestamps, but falls back to raw opus data if rate-limited.
    """
    all_raw_items: list[dict[str, Any]] = []
    offset = ""

    for _ in range(max_pages):
        base_params: dict[str, Any] = {
            "host_mid": uid, "page": 1, "offset": offset,
            "type": "all", "timezone_offset": -480,
        }
        params = _sign_wbi_params(base_params, img_key, sub_key)
        payload = _http_get_json(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/feed/space",
            params,
        )
        if payload.get("code") != 0:
            break

        data = payload.get("data") or {}
        raw_items = data.get("items") or []
        all_raw_items.extend(raw_items)

        if not data.get("has_more"):
            break
        offset = data.get("offset") or ""
        if not offset:
            break

        time.sleep(0.3)

    if not all_raw_items:
        return []

    full_items: list[dict[str, Any]] = []
    opus_content_map: dict[str, str] = {}
    html_opus_cards = _fetch_space_dynamic_html_opus_cards(uid)
    for raw in all_raw_items:
        opus_id = raw.get("opus_id")
        if not opus_id:
            continue
        html_card = html_opus_cards.get(str(opus_id)) or {}
        if html_card.get("title"):
            raw["_html_title"] = html_card["title"]
        if html_card.get("text"):
            raw["_html_content"] = html_card["text"]
        content = raw.get("content", "")
        if content:
            opus_content_map[opus_id] = content
        raw["_opus_content"] = content
        try:
            detail_payload = _http_get_json(
                "https://api.bilibili.com/x/polymer/web-dynamic/desktop/v1/detail",
                {"id": opus_id},
            )
            if detail_payload.get("code") == 0:
                item = (detail_payload.get("data") or {}).get("item")
                if item:
                    if opus_id in opus_content_map:
                        item["_opus_content"] = opus_content_map[opus_id]
                    if html_card.get("title"):
                        item["_html_title"] = html_card["title"]
                    if html_card.get("text"):
                        item["_html_content"] = html_card["text"]
                    full_items.append(item)
                    continue
            web_item = _fetch_opus_web_detail(str(opus_id))
            if web_item:
                if opus_id in opus_content_map:
                    web_item["_opus_content"] = opus_content_map[opus_id]
                if html_card.get("title"):
                    web_item["_html_title"] = html_card["title"]
                if html_card.get("text"):
                    web_item["_html_content"] = html_card["text"]
                full_items.append(web_item)
                continue
            full_items.append(raw)
        except Exception as exc:
            logger.debug(f"Dynamic detail fetch failed for {opus_id}: {exc}")
            try:
                web_item = _fetch_opus_web_detail(str(opus_id))
                if web_item:
                    if opus_id in opus_content_map:
                        web_item["_opus_content"] = opus_content_map[opus_id]
                    if html_card.get("title"):
                        web_item["_html_title"] = html_card["title"]
                    if html_card.get("text"):
                        web_item["_html_content"] = html_card["text"]
                    full_items.append(web_item)
                    continue
            except Exception as web_exc:
                logger.debug(f"Opus web detail fallback failed for {opus_id}: {web_exc}")
            full_items.append(raw)

    _backfill_ordered_opus_pub_ts(full_items, since_ts)
    return full_items


def _parse_opus_item(raw: dict[str, Any], uid: str, author_name: str) -> dict[str, Any]:
    opus_id = raw.get("opus_id", "")
    content = (raw.get("_html_content") or raw.get("_opus_content") or raw.get("content", "") or "").strip()
    cover = raw.get("cover") or {}
    cover_url = _canonical_url(cover.get("url"))
    stat = raw.get("stat") or {}

    opus_link = f"https://www.bilibili.com/opus/{opus_id}" if opus_id else None

    title = (raw.get("_html_title") or raw.get("title") or "").strip()

    pub_ts = raw.get("pub_ts") or raw.get("ctime") or raw.get("pub_time") or raw.get("_estimated_pub_ts") or None
    if pub_ts is not None:
        try:
            pub_ts = int(pub_ts)
        except (ValueError, TypeError):
            pub_ts = None

    if pub_ts is None:
        pub_ts = _pub_ts_from_dynamic_id(opus_id)

    # Fallback: if no timestamp in raw opus data, try to fetch from detail API
    if pub_ts is None and opus_id:
        try:
            detail_payload = _http_get_json(
                "https://api.bilibili.com/x/polymer/web-dynamic/desktop/v1/detail",
                {"id": opus_id},
            )
            if detail_payload.get("code") == 0:
                item = (detail_payload.get("data") or {}).get("item")
                if item:
                    modules = _module_map(item.get("modules") or {})
                    author = modules.get("module_author") or {}
                    detail_pub_ts = author.get("pub_ts")
                    if detail_pub_ts:
                        pub_ts = int(detail_pub_ts)
        except Exception as exc:
            logger.debug(f"Failed to fetch detail for opus {opus_id} timestamp: {exc}")

    images: list[str] = []
    for pic_obj in (raw.get("pics") or []):
        url = pic_obj.get("url") or pic_obj.get("src") or ""
        if url:
            images.append(_canonical_url(url) or url)
    if not images and cover_url:
        images.append(cover_url)

    if not content:
        summary = raw.get("summary") or {}
        content = summary.get("text", "")

    return {
        "dynamic_id": opus_id,
        "type": raw.get("type", "DYNAMIC_TYPE_DRAW"),
        "title": title,
        "author_name": author_name,
        "author_mid": uid,
        "pub_ts": pub_ts,
        "pub_time": None,
        "pub_time_utc": _iso_utc(pub_ts),
        "link": opus_link,
        "text": content,
        "attached_video": None,
        "images": images,
        "source": "opus_feed",
    }


def _discover_forward_dynamics(
    uid: str, existing_ids: set[str], img_key: str, sub_key: str, since_ts: int,
    opus_contents: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Discover FORWARD (repost) dynamics via the space search API.

    The opus feed API only returns DRAW/AV types. The search API with
    content-derived keywords can return FORWARD dynamics too.
    """
    forward_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set(existing_ids)

    keywords: list[str] = []
    if opus_contents:
        for content in opus_contents:
            clean = re.sub(r"\s+", "", content)
            if len(clean) >= 4:
                keywords.append(clean[:4])
    if not keywords:
        keywords = ["的", "了", "是"]

    for keyword in keywords:
        try:
            params = _sign_wbi_params(
                {
                    "host_mid": uid, "page": 1, "offset": "",
                    "keyword": keyword, "features": FEATURES,
                    "timezone_offset": -480, "platform": "web",
                },
                img_key, sub_key,
            )
            payload = _http_get_json(
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space/search",
                params,
            )
            if payload.get("code") != 0:
                continue
            items = ((payload.get("data") or {}).get("items") or [])
            for item in items:
                item_id = item.get("id_str", "")
                if not item_id or item_id in seen_ids:
                    continue
                author = _module_map(item.get("modules") or {}).get("module_author") or {}
                if str(author.get("mid")) != str(uid):
                    continue
                pub_ts = author.get("pub_ts")
                if not pub_ts or int(pub_ts) < since_ts:
                    continue
                seen_ids.add(item_id)
                forward_items.append(item)
        except Exception as exc:
            logger.debug(f"Forward discovery via search failed for keyword {keyword!r}: {exc}")

    return forward_items


def _search_dynamic_by_keyword(
    uid: str, keyword: str, img_key: str, sub_key: str
) -> list[dict[str, Any]]:
    params = _sign_wbi_params(
        {
            "host_mid": uid, "page": 1, "offset": "",
            "keyword": keyword, "features": FEATURES,
            "timezone_offset": -480, "platform": "web",
        },
        img_key, sub_key,
    )
    payload = _http_get_json(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space/search",
        params,
    )
    if payload.get("code") != 0:
        raise RuntimeError(
            f"feed/space/search returned code={payload.get('code')} message={payload.get('message')}"
        )
    return ((payload.get("data") or {}).get("items") or [])


def _build_dynamic_keywords(title: str) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()

    def add(keyword: str) -> None:
        value = keyword.strip()
        if not value or value in seen:
            return
        seen.add(value)
        keywords.append(value)

    add(title)
    for token in re.findall(r"[A-Za-z0-9]{3,}", title):
        add(token)
    return keywords


def find_video_linked_dynamics(
    uid: str,
    videos: list[dict[str, Any]],
    since_ts: int,
    img_key: str,
    sub_key: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Find dynamics that link to recent videos via keyword search."""
    matches: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()

    for video in videos:
        title = video.get("title") or ""
        bvid = video.get("bvid")
        if not title or not bvid:
            continue

        matched_current_video = False
        for keyword in _build_dynamic_keywords(title):
            try:
                items = _search_dynamic_by_keyword(uid, keyword, img_key, sub_key)
            except Exception as exc:
                warnings.append(f"dynamic keyword search failed for {title} with {keyword!r}: {exc}")
                continue

            for item in items:
                parsed = _parse_dynamic_item(item, "dynamic_search")
                attached = parsed.get("attached_video") or {}
                if attached.get("bvid") != bvid:
                    continue
                dynamic_id = parsed.get("dynamic_id")
                if dynamic_id and dynamic_id in seen_ids:
                    continue
                if not parsed.get("pub_ts") or int(parsed["pub_ts"]) < since_ts:
                    continue
                if dynamic_id:
                    seen_ids.add(dynamic_id)
                matches.append(parsed)
                matched_current_video = True

            if matched_current_video:
                break

    return matches, warnings


# ---- Main Fetch Function ----

def fetch_up_updates(
    uid: str,
    since_ts: int,
    video_pages: int = 2,
    video_page_size: int = 10,
    author_name: str = "",
) -> dict[str, Any]:
    """Fetch all recent updates for a single UP.

    Returns a result dict with videos, dynamics, status, and warnings.
    """
    warnings: list[str] = []

    # Fetch recent videos
    videos, video_meta = fetch_recent_videos(
        uid, since_ts, video_pages, video_page_size, author_name=author_name,
    )
    warnings.extend(video_meta.get("warnings") or [])
    video_status = video_meta.get("status") or "unverified-no-cookie"
    video_source = video_meta.get("source")

    # Fetch dynamics
    has_cookie = bool(_global_cookie_dict.get("SESSDATA"))
    img_key, sub_key = "", ""
    try:
        img_key, sub_key = _fetch_wbi_keys()
    except Exception as exc:
        warnings.append(f"WBI key fetch failed: {exc}")

    full_dynamic_items: list[dict[str, Any]] = []
    used_cookie_feed = False

    if has_cookie:
        try:
            cookie_items = fetch_dynamic_feed_with_cookie(uid, since_ts=since_ts)
            for item in cookie_items:
                if item.get("modules"):
                    parsed = _parse_dynamic_item(item, "cookie_feed")
                    pub_ts = parsed.get("pub_ts")
                    if not pub_ts or int(pub_ts) < since_ts:
                        continue
                    full_dynamic_items.append(parsed)
                elif item.get("opus_id"):
                    parsed = _parse_opus_item(item, uid, author_name)
                    pub_ts = parsed.get("pub_ts")
                    if not pub_ts or int(pub_ts) < since_ts:
                        continue
                    full_dynamic_items.append(parsed)
            if cookie_items:
                used_cookie_feed = True
        except Exception as exc:
            logger.debug(f"Cookie feed API failed, falling back to public: {exc}")

    if not used_cookie_feed and img_key and sub_key:
        try:
            probe_items = fetch_dynamic_feed_probe(uid, img_key, sub_key, since_ts=since_ts)
            for item in probe_items:
                if item.get("modules"):
                    parsed = _parse_dynamic_item(item, "dynamic_feed_probe")
                    pub_ts = parsed.get("pub_ts")
                    if not pub_ts or int(pub_ts) < since_ts:
                        continue
                    full_dynamic_items.append(parsed)
                elif item.get("opus_id"):
                    parsed = _parse_opus_item(item, uid, author_name)
                    pub_ts = parsed.get("pub_ts")
                    if not pub_ts or int(pub_ts) < since_ts:
                        continue
                    full_dynamic_items.append(parsed)
        except Exception as exc:
            warnings.append(f"public dynamic feed probe failed: {exc}")

        # Discover FORWARD (repost) dynamics via search API
        known_dynamic_ids = {d.get("dynamic_id", "") for d in full_dynamic_items if d.get("dynamic_id")}
        opus_contents = [d.get("text", "") for d in full_dynamic_items if d.get("text")]
        try:
            forward_items = _discover_forward_dynamics(
                uid, known_dynamic_ids, img_key, sub_key, since_ts,
                opus_contents=opus_contents,
            )
            for fwd_item in forward_items:
                parsed = _parse_dynamic_item(fwd_item, "forward_discovery")
                pub_ts = parsed.get("pub_ts")
                if not pub_ts or int(pub_ts) < since_ts:
                    continue
                full_dynamic_items.append(parsed)
        except Exception as exc:
            logger.debug(f"Forward discovery failed: {exc}")

    if img_key and sub_key:
        video_linked_dynamics, dynamic_search_warnings = find_video_linked_dynamics(
            uid, videos, since_ts, img_key, sub_key,
        )
        warnings.extend(dynamic_search_warnings)
    else:
        video_linked_dynamics = []

    # Deduplicate dynamics
    deduped_dynamics: list[dict[str, Any]] = []
    seen_dynamic_ids: set[str] = set()
    for item in full_dynamic_items + video_linked_dynamics:
        dynamic_id = item.get("dynamic_id")
        if dynamic_id and dynamic_id in seen_dynamic_ids:
            continue
        if dynamic_id:
            seen_dynamic_ids.add(dynamic_id)
        deduped_dynamics.append(item)

    # Determine dynamic coverage status
    if used_cookie_feed:
        dynamic_status = "cookie-feed-complete"
    elif full_dynamic_items:
        dynamic_status = "feed-probe-best-effort"
    elif deduped_dynamics:
        dynamic_status = "video-linked-only"
    else:
        dynamic_status = "unverified-no-cookie"

    partial = video_status != "ok"

    return {
        "uid": uid,
        "videos": videos,
        "dynamics": deduped_dynamics,
        "status": {
            "videos": video_status,
            "video_source": video_source,
            "dynamics": dynamic_status,
            "result_is_partial": partial,
        },
        "warnings": warnings,
    }

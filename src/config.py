"""Configuration loader for Bilibili UP Monitor."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _parse_interval(value: str) -> int:
    """Parse interval string like '1m', '5m', '30m', '1h' into seconds."""
    value = value.strip().lower()
    match = re.match(r"^(\d+)\s*(m|h|s)?$", value)
    if not match:
        raise ValueError(f"Invalid interval format: {value!r}. Use e.g. 1m, 5m, 30m, 1h")
    amount = int(match.group(1))
    unit = match.group(2) or "m"
    if unit == "s":
        return amount
    elif unit == "m":
        return amount * 60
    elif unit == "h":
        return amount * 3600
    raise ValueError(f"Unknown interval unit: {unit!r}")


@dataclass
class MonitorConfig:
    """All runtime configuration for the monitor."""

    # Target UIDs
    target_uids: list[str] = field(default_factory=list)

    # Monitor interval in seconds
    interval_seconds: int = 300  # 5m

    # Database
    db_path: str = "data/monitor.db"

    # Feishu
    enable_feishu: bool = False
    feishu_webhook_url: str = ""
    feishu_webhook_secret: str = ""
    feishu_app_id: str = ""
    feishu_app_secret: str = ""

    # Local output
    enable_local_output: bool = True
    local_output_dir: str = "data/output"

    # Video download
    enable_video_download: bool = False
    video_download_dir: str = "data/videos"
    video_download_mode: str = "analysis-fast"

    # General
    timezone_offset: int = -480
    retention_days: int = 30
    log_level: str = "INFO"

    # Project base dir (resolved at runtime)
    base_dir: Path = field(default_factory=lambda: Path.cwd())

    def resolve_path(self, path: str) -> Path:
        """Resolve a path relative to base_dir if not absolute."""
        p = Path(path)
        if p.is_absolute():
            return p
        return self.base_dir / p


def load_config(env_file: Optional[str] = None) -> MonitorConfig:
    """Load configuration from environment and optional .env file."""
    # Try loading .env
    if env_file and Path(env_file).exists():
        try:
            from dotenv import dotenv_values
            env_values = dotenv_values(env_file)
            for key, value in env_values.items():
                if key and value is not None:
                    # An explicit config file is the source of truth for this
                    # app. Android processes can keep blank/default values in
                    # os.environ, which would otherwise mask saved settings.
                    os.environ[key] = value
        except ImportError:
            pass  # python-dotenv not installed, rely on env only

    cfg = MonitorConfig()

    # Target UIDs
    raw_uids = os.environ.get("BILIBILI_TARGET_UIDS", "")
    if raw_uids:
        for token in raw_uids.split(","):
            token = token.strip()
            if not token:
                continue
            # Support space URL input
            match = re.search(r"(?:space\.bilibili\.com|m\.bilibili\.com/space|bilibili\.com/space)/(\d+)", token)
            if match:
                cfg.target_uids.append(match.group(1))
            elif token.isdigit():
                cfg.target_uids.append(token)

    # Interval
    interval_str = os.environ.get("MONITOR_INTERVAL", "5m")
    cfg.interval_seconds = _parse_interval(interval_str)

    # Database
    cfg.db_path = os.environ.get("DB_PATH", cfg.db_path)

    # Feishu
    cfg.enable_feishu = os.environ.get("ENABLE_FEISHU", "false").lower() in ("true", "1", "yes")
    cfg.feishu_webhook_url = os.environ.get("FEISHU_WEBHOOK_URL", "")
    cfg.feishu_webhook_secret = os.environ.get("FEISHU_WEBHOOK_SECRET", "")
    cfg.feishu_app_id = os.environ.get("FEISHU_APP_ID", "")
    cfg.feishu_app_secret = os.environ.get("FEISHU_APP_SECRET", "")

    # Local output
    cfg.enable_local_output = os.environ.get("ENABLE_LOCAL_OUTPUT", "true").lower() in ("true", "1", "yes")
    cfg.local_output_dir = os.environ.get("LOCAL_OUTPUT_DIR", cfg.local_output_dir)

    # Video download
    cfg.enable_video_download = os.environ.get("ENABLE_VIDEO_DOWNLOAD", "false").lower() in ("true", "1", "yes")
    cfg.video_download_dir = os.environ.get("VIDEO_DOWNLOAD_DIR", cfg.video_download_dir)
    cfg.video_download_mode = os.environ.get("VIDEO_DOWNLOAD_MODE", cfg.video_download_mode)

    # General
    cfg.timezone_offset = int(os.environ.get("TIMEZONE_OFFSET", str(cfg.timezone_offset)))
    cfg.retention_days = int(os.environ.get("RETENTION_DAYS", str(cfg.retention_days)))
    cfg.log_level = os.environ.get("LOG_LEVEL", cfg.log_level)

    return cfg

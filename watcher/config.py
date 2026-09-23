from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Config:
    handle: str = "thsottiaux"
    include_replies: bool = True
    watch_changelog: bool = True

    interval_min: int = 10
    score_threshold: int = 3
    alert_all: bool = False
    state_file: Path = PROJECT_ROOT / "watcher_state.json"

    dayclaw_url: str = "https://api.dayclaw.com/api/source/public/x/{handle}/items"
    changelog_url: str = "https://learn.chatgpt.com/docs/changelog"

    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""
    mail_to: str = ""
    pushplus_token: str = ""
    pushplus_channel: str = "wechat"

    verbose: bool = False

    def active_channels(self) -> list[str]:
        channels = []
        if self.pushplus_token:
            channels.append("pushplus")
        if self.ntfy_topic:
            channels.append("ntfy")
        if self.telegram_token and self.telegram_chat_id:
            channels.append("telegram")
        if self.smtp_host and self.mail_to:
            channels.append("email")
        return channels


def load_config(**overrides) -> Config:
    _load_dotenv(PROJECT_ROOT / ".env")
    env = os.environ.get
    cfg = Config(
        handle=(env("TIBO_HANDLE", "") or Config.handle).lstrip("@"),
        include_replies=_flag("INCLUDE_REPLIES", "1"),
        watch_changelog=_flag("WATCH_CHANGELOG", "1"),
        interval_min=max(1, _int_env("WATCH_INTERVAL_MIN", 10)),
        score_threshold=_int_env("SCORE_THRESHOLD", 3),
        alert_all=_flag("ALERT_ALL", "0"),
        state_file=Path(env("STATE_FILE", "") or str(Config.state_file)),
        ntfy_server=env("NTFY_SERVER", Config.ntfy_server) or Config.ntfy_server,
        ntfy_topic=env("NTFY_TOPIC", "").strip(),
        telegram_token=env("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=env("TELEGRAM_CHAT_ID", "").strip(),
        smtp_host=env("SMTP_HOST", "").strip(),
        smtp_port=_int_env("SMTP_PORT", 587),
        smtp_user=env("SMTP_USER", "").strip(),
        smtp_password=env("SMTP_PASSWORD", "").strip(),
        mail_from=env("MAIL_FROM", env("SMTP_USER", "")).strip(),
        mail_to=env("MAIL_TO", "").strip(),
        pushplus_token=env("PUSHPLUS_TOKEN", "").strip(),
        pushplus_channel=env("PUSHPLUS_CHANNEL", "wechat").strip() or "wechat",
        verbose=_flag("VERBOSE", "0"),
    )
    for key, value in overrides.items():
        if value is not None and hasattr(cfg, key):
            setattr(cfg, key, value)
    cfg.state_file = Path(cfg.state_file)
    return cfg

from __future__ import annotations

import json
import smtplib
import ssl
from email.message import EmailMessage

from .config import Config
from .fetch import http_post
from .model import Post


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def format_title(post: Post) -> str:
    return "Tibo 通知测试" if post.key == "test-ping" else f"Tibo 重置动态：{_clip(post.title, 80)}"


def format_alert(cfg: Config, post: Post, score_total: int, score_hits: str, *, long: bool = False) -> str:
    label = "通知通道测试（不是重置公告）" if post.key == "test-ping" else "额度重置相关信号（未核实到账）"
    return f"{label}\n时间：{post.created_at or '未知'}\n{post.url}\n\n{_clip(post.body, 1800 if long else 500)}"


def _send_ntfy(cfg: Config, title: str, body: str, url: str) -> tuple[bool, str]:
    status, _ = http_post(f"{cfg.ntfy_server.rstrip('/')}/{cfg.ntfy_topic}", body.encode("utf-8"), headers={
        "Title": title.encode("utf-8"), "Priority": "high", "Tags": "arrow_counter_clockwise", "Click": url.encode("utf-8"),
    })
    return 200 <= status < 300, f"ntfy HTTP {status}"


def _send_telegram(cfg: Config, title: str, body: str, url: str) -> tuple[bool, str]:
    payload = json.dumps({"chat_id": cfg.telegram_chat_id, "text": f"{title}\n\n{body}", "disable_web_page_preview": True}).encode("utf-8")
    status, response = http_post(f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage", payload, headers={"Content-Type": "application/json"})
    try:
        data = json.loads(response)
        ok = 200 <= status < 300 and isinstance(data, dict) and data.get("ok") is True
    except (TypeError, ValueError):
        ok = False
    return ok, f"telegram HTTP {status}"


def _send_email(cfg: Config, title: str, body: str) -> tuple[bool, str]:
    message = EmailMessage()
    message["Subject"] = title
    message["From"] = cfg.mail_from or cfg.smtp_user
    message["To"] = cfg.mail_to
    message.set_content(body)
    context = ssl.create_default_context()
    if cfg.smtp_port == 465:
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=25, context=context)
        with server:
            server.login(cfg.smtp_user, cfg.smtp_password)
            refused = server.send_message(message)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=25)
        with server:
            server.starttls(context=context)
            server.login(cfg.smtp_user, cfg.smtp_password)
            refused = server.send_message(message)
    return not bool(refused), "email accepted by SMTP server" if not refused else "email recipient refused"


def _send_pushplus(cfg: Config, post: Post, score_total: int, score_hits: str) -> tuple[bool, str]:
    payload = json.dumps({
        "token": cfg.pushplus_token, "title": format_title(post),
        "content": format_alert(cfg, post, score_total, score_hits, long=True),
        "template": "txt", "channel": cfg.pushplus_channel,
    }, ensure_ascii=False).encode("utf-8")
    status, response = http_post("https://www.pushplus.plus/send", payload, headers={"Content-Type": "application/json"})
    try:
        data = json.loads(response)
    except (TypeError, ValueError):
        return False, "pushplus invalid response"
    code = data.get("code") if isinstance(data, dict) else None
    ok = 200 <= status < 300 and type(code) in (int, float) and code == 200
    return ok, "pushplus accepted; phone receipt not yet verified" if ok else "pushplus rejected the message"


def send_channel(cfg: Config, channel: str, post: Post, score_total: int, score_hits: str) -> tuple[bool, str]:
    # Never return raw response/exception text: providers can echo private values.
    try:
        title = format_title(post)
        body = format_alert(cfg, post, score_total, score_hits, long=True)
        if channel == "ntfy":
            return _send_ntfy(cfg, title, format_alert(cfg, post, score_total, score_hits), post.url)
        if channel == "telegram":
            return _send_telegram(cfg, title, body, post.url)
        if channel == "email":
            return _send_email(cfg, title, body)
        if channel == "pushplus":
            return _send_pushplus(cfg, post, score_total, score_hits)
        return False, "unknown channel"
    except Exception as error:
        return False, f"notification failed: {type(error).__name__}"


def send_alert(cfg: Config, post: Post, score_total: int, score_hits: str) -> list[tuple[bool, str]]:
    return [send_channel(cfg, channel, post, score_total, score_hits) for channel in cfg.active_channels()]


def send_test(cfg: Config) -> list[tuple[bool, str]]:
    post = Post(key="test-ping", source="x", title="通知测试", body="这是一条测试通知。如果手机能看到它，推送通道已连通。它不是重置公告。", url="https://x.com/" + cfg.handle, created_at="测试发送时间")
    return send_alert(cfg, post, 0, "test")

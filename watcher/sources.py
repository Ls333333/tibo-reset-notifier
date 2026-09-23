from __future__ import annotations

import html as html_utils
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .config import Config
from .fetch import USER_AGENT, get_json, http_get
from .model import Post

AIHOT_URL = "https://aihot.news/api/v1/codex-resets"
_POST_URL = re.compile(r"https://x\.com/thsottiaux/status/([0-9]+)")
_AIHOT_CACHE: dict[str, dict] = {}


def _max_source_age() -> timedelta:
    try:
        hours = float(os.environ.get("SOURCE_MAX_AGE_HOURS", "6"))
        if not math.isfinite(hours) or hours <= 0:
            raise ValueError
        return timedelta(hours=hours)
    except (ValueError, OverflowError) as error:
        raise ValueError("SOURCE_MAX_AGE_HOURS must be a finite positive number") from error


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"AIHOT {field} must be an ISO timestamp with timezone")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"AIHOT {field} is not a valid ISO timestamp") from error
    if stamp.tzinfo is None:
        raise ValueError(f"AIHOT {field} must include a timezone")
    return stamp


def _string(record: dict, field: str, *, empty: bool = False) -> str:
    value = record.get(field)
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"AIHOT {field} must be a {'possibly empty ' if empty else ''}string")
    return value


def _parse_aihot(data: object, *, now: datetime | None = None) -> list[Post]:
    """Validate the documented snapshot and emit original posts, not event updates."""
    if not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1:
        raise ValueError("AIHOT unsupported or missing schemaVersion (expected 1)")
    if data.get("timezone") != "Asia/Shanghai":
        raise ValueError("AIHOT unexpected or missing timezone")
    if any(data.get(key) is True for key in ("stale", "isStale", "is_stale")) or data.get("status") == "stale":
        raise ValueError("AIHOT explicitly reports stale upstream data")
    checked = _timestamp(data.get("checkedAt"), "checkedAt")
    current = now or datetime.now(timezone.utc)
    max_age = _max_source_age()
    if current - checked > max_age:
        raise ValueError(f"AIHOT upstream verification is stale: checkedAt={checked.isoformat()} (older than {max_age.total_seconds() / 3600:g} hours)")
    if checked - current > timedelta(minutes=10):
        raise ValueError("AIHOT checkedAt is unexpectedly in the future")
    _timestamp(data.get("historyFrom"), "historyFrom")
    events = data.get("events")
    if not isinstance(events, list) or type(data.get("count")) is not int or data["count"] != len(events):
        raise ValueError("AIHOT events/count schema mismatch")

    posts: dict[str, Post] = {}
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("AIHOT event must be an object")
        _string(event, "id")
        _string(event, "title")
        _string(event, "url")
        _string(event, "scope", empty=True)
        if event.get("type") not in ("direct_reset", "reset_credit"):
            raise ValueError("AIHOT unknown reset event type")
        if event.get("label") not in ("全员重置", "发重置卡"):
            raise ValueError("AIHOT unknown reset event label")
        if event.get("status") not in ("announced", "confirmed"):
            raise ValueError("AIHOT unknown reset event status")
        for field in ("createdAt", "updatedAt"):
            _timestamp(event.get(field), field)
        for field in ("confirmedAt", "occurredOn", "confirmationBasis", "schedule"):
            if field not in event:
                raise ValueError(f"AIHOT missing event field {field}")
        if event["confirmedAt"] is not None:
            _timestamp(event["confirmedAt"], "confirmedAt")
        if event["occurredOn"] is not None:
            value = event["occurredOn"]
            if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
                raise ValueError("AIHOT occurredOn must be a date or null")
            datetime.strptime(value, "%Y-%m-%d")
        if event["confirmationBasis"] not in (None, "source_post", "receipt_review"):
            raise ValueError("AIHOT unknown confirmationBasis")
        schedule = event["schedule"]
        if schedule is not None:
            if not isinstance(schedule, dict) or schedule.get("precision") not in ("exact", "approximate", "deadline", "date", "window"):
                raise ValueError("AIHOT invalid schedule")
            _string(schedule, "label")
            beginning = _timestamp(schedule.get("from"), "schedule.from")
            ending = _timestamp(schedule.get("through"), "schedule.through")
            if ending < beginning:
                raise ValueError("AIHOT schedule ends before it starts")
        entries = event.get("posts")
        if not isinstance(entries, list):
            raise ValueError("AIHOT posts must be an array")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("AIHOT post must be an object")
            post_id = _string(entry, "id")
            url = _string(entry, "url")
            match = _POST_URL.fullmatch(url)
            if match is None or match.group(1) != post_id:
                raise ValueError("AIHOT post must link to the matching @thsottiaux X post")
            for field in ("author", "author_user_name"):
                if field in entry and (not isinstance(entry[field], str) or entry[field].lstrip("@").lower() != "thsottiaux"):
                    raise ValueError("AIHOT post author does not match @thsottiaux")
            stamp = _timestamp(entry.get("publishedAt"), "publishedAt")
            stage = _string(entry, "stage")
            translated = _string(entry, "text")
            original = _string(entry, "originalText")
            # The same X post can appear in several regrouped calendar events.
            # Stable original IDs also prevent receipt reviews from inventing posts.
            if post_id in posts:
                continue
            posts[post_id] = Post(
                key=f"aihot:{post_id}", source="x",
                title=f"Tibo · {event['label']} · {stage}",
                body=f"AIHOT整理 · {stage}\n{translated}\n\n原文摘录：\n{original}",
                url=url, created_at=stamp.isoformat(),
                # This API includes replies but does not expose a reply flag.
                is_reply=False,
            )
    return sorted(posts.values(), key=lambda post: _timestamp(post.created_at, "publishedAt"))


def _retry_after(value: str | None) -> float:
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError):
            return 0


def fetch_aihot_posts(cfg: Config) -> list[Post]:
    if cfg.handle.lstrip("@").lower() != "thsottiaux":
        raise ValueError("The AIHOT reset feed only monitors @thsottiaux")
    url = getattr(cfg, "aihot_url", AIHOT_URL)
    # Memory only: dry-run never changes files. Long-running polling reuses the
    # ETag; a scheduled Actions process begins with an ordinary cold request.
    cache = _AIHOT_CACHE.get(url, {})
    age = time.time() - cache.get("fetched_at", 0)
    if cache and 0 <= age < max(300, cache["min_interval"]):
        return _parse_aihot(cache["data"])
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if cache.get("etag"):
        headers["If-None-Match"] = cache["etag"]
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=25) as response:
                data = json.loads(response.read().decode("utf-8"))
                response_headers = response.headers
            posts = _parse_aihot(data)
            max_age = re.search(r"(?:^|,)\s*s-maxage=(\d+)", response_headers.get("Cache-Control", ""))
            cache = {"url": url, "etag": response_headers.get("ETag", ""), "data": data,
                     "fetched_at": time.time(), "min_interval": max(300, int(max_age.group(1))) if max_age else 300}
            _AIHOT_CACHE[url] = cache
            return posts
        except urllib.error.HTTPError as error:
            if error.code == 304:
                if not cache:
                    raise RuntimeError("AIHOT returned 304 without a cached snapshot") from error
                posts = _parse_aihot(cache["data"])
                cache["fetched_at"] = time.time()
                _AIHOT_CACHE[url] = cache
                return posts
            delay = max(2 ** (attempt + 1), _retry_after(error.headers.get("Retry-After")))
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2 or delay > 30:
                raise RuntimeError(f"AIHOT HTTP {error.code}; retry on the next scheduled run (Retry-After={error.headers.get('Retry-After', 'none')})") from error
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt == 2:
                raise RuntimeError("AIHOT fetch failed after 3 attempts") from error
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError("AIHOT fetch failed")


def fetch_x_posts(cfg: Config) -> list[Post]:
    url = cfg.dayclaw_url.format(handle=cfg.handle)
    data = get_json(url)
    items = data.get("items") or []
    posts: list[Post] = []
    for item in items:
        external_id = str(item.get("external_id") or item.get("id") or "")
        text = item.get("content") or item.get("title") or ""
        if not external_id or not text:
            continue
        metadata = item.get("metadata") or {}
        posts.append(Post(
            key=f"dayclaw:{external_id}",
            source="x",
            title=" ".join(text.split()),
            body=html_utils.unescape(text),
            url=item.get("url") or f"https://x.com/{cfg.handle}/status/{external_id}",
            created_at=item.get("published_at") or "",
            is_reply=bool(metadata.get("is_reply") or metadata.get("isReply")),
        ))
    return posts


_LI_ID_RE = re.compile(r'<li id="([^"]+)"')
_TIME_RE = re.compile(r"<time[^>]*>\s*([^<]+?)\s*</time>")
_HEADING_RE = re.compile(r"<h[1-4][^>]*>(.*?)</h[1-4]>", re.S)
_ARTICLE_RE = re.compile(r"<article\b")
_TAG_RE = re.compile(r"""<(?:[^>"']|"[^"]*"|'[^']*')*>""")
_MAX_ENTRIES = 40


def _plain_text(fragment: str) -> str:
    no_tags = _TAG_RE.sub(" ", fragment)
    return " ".join(html_utils.unescape(no_tags).split())


def fetch_changelog_entries(cfg: Config) -> list[Post]:
    page = http_get(cfg.changelog_url)
    marks = list(_LI_ID_RE.finditer(page))
    entries: list[Post] = []
    spans = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(page)
        spans.append((mark.group(1), page[mark.start():end]))
    for li_id, span in spans:
        if _ARTICLE_RE.search(span) is None or len(entries) >= _MAX_ENTRIES:
            continue
        time_match = _TIME_RE.search(span)
        heading_match = _HEADING_RE.search(span)
        article_match = _ARTICLE_RE.search(span)
        body_html = span[article_match.start():span.find("</article>",
                                                        article_match.start())]
        date = _plain_text(time_match.group(1)) if time_match else ""
        title = _plain_text(heading_match.group(1)) if heading_match else "Changelog update"
        body = _plain_text(body_html)
        if not body:
            continue
        entries.append(Post(
            key=f"changelog:{li_id}",
            source="changelog",
            title=title,
            body=body,
            url=cfg.changelog_url,
            created_at=date,
        ))
    return entries


def collect(cfg: Config) -> list[Post]:
    posts: list[Post] = []
    # AIHOT is the required primary source. A healthy unrelated changelog must
    # never conceal a stale or broken Tibo feed from the scheduler.
    posts.extend(fetch_aihot_posts(cfg))
    if cfg.watch_changelog:
        try:
            posts.extend(fetch_changelog_entries(cfg))
        except Exception as error:
            print(f"[warn] changelog source failed: {error}")
    return posts

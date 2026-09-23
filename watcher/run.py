from __future__ import annotations

import time
from datetime import datetime, timezone

from . import notify, sources
from .config import Config
from .detect import score_text, should_alert
from .state import StateStore
from .model import Post

MAX_ALERTS_PER_CYCLE = 5


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def log(message: str) -> None:
    print(f"[{_stamp()}] {message}", flush=True)


def check_once(cfg: Config, *, prime: bool = False, dry_run: bool = False) -> int:
    store = StateStore(cfg.state_file)
    source_error = None
    try:
        posts = sources.collect(cfg)
    except Exception as error:
        source_error = error
        posts = []
        log(f"source check failed ({type(error).__name__}); retrying previously queued alerts")
    baseline = store.fresh_file or prime
    channels = cfg.active_channels()
    findings = 0
    for post in posts:
        if store.is_seen(post.key):
            continue
        score = score_text(post.body)
        matched = (cfg.include_replies or not post.is_reply) and (
            cfg.alert_all or post.key.startswith("aihot:") or should_alert(score, cfg.score_threshold)
        )
        if baseline or not matched:
            store.mark_seen(post.key)
        elif channels:
            store.enqueue(post, score.total, score.hits, channels)
            findings += 1
        else:
            log("new alert retained in source: configure a notification channel")
    if baseline:
        log(f"baseline recorded: {store.tracked_count()} items tracked, "
            "no historical alerts queued")
    if dry_run:
        log(f"DRY-RUN: {len(posts)} posts, {findings} new alerts; no sends or state changes")
        if source_error:
            raise source_error
        return 0
    # The durable queue and source dedupe advance in one atomic state write.
    if not source_error or not store.fresh_file:
        store.save()
    if prime:
        if source_error:
            raise source_error
        return 0
    pushed = 0
    attempted_posts = 0
    for key, record in list(store.pending.items()):
        active_pending = [c for c in record["remaining"] if c in channels]
        if not active_pending:
            continue
        if attempted_posts >= MAX_ALERTS_PER_CYCLE:
            break
        attempted_posts += 1
        post = Post(**record["post"])
        for channel in active_pending:
            try:
                ok, note = notify.send_channel(cfg, channel, post, record["score_total"], record["score_hits"])
            except Exception as error:
                ok, note = False, type(error).__name__
            if ok:
                finished = store.complete_channel(key, channel)
                store.save()
                pushed += int(finished)
                log(f"{channel}: provider accepted notification")
            else:
                # Notes are deliberately not printed: third-party responses may echo credentials.
                log(f"{channel}: delivery failed; kept for a later run")
    log(f"cycle done: {len(posts)} fetched, {findings} queued, {pushed} completed, {len(store.pending)} pending")
    if source_error:
        raise source_error
    return pushed


def run_loop(cfg: Config) -> None:
    interval = cfg.interval_min * 60
    channels = ", ".join(cfg.active_channels()) or "NONE (console only!)"
    log(f"loop started: every {cfg.interval_min} min, watching "
        f"@{cfg.handle}, channels: {channels}")
    while True:
        try:
            check_once(cfg)
        except KeyboardInterrupt:
            log("interrupted; bye")
            return
        except Exception as error:
            log(f"cycle crashed but loop continues: {error}")
        time.sleep(interval)

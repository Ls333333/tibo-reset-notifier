from __future__ import annotations

import copy
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from watcher.config import Config
from watcher import sources


NOW = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)


def snapshot() -> dict:
    return {
        "schemaVersion": 1,
        "timezone": "Asia/Shanghai",
        "checkedAt": NOW.isoformat(),
        "historyFrom": "2026-06-12T00:00:00+08:00",
        "count": 1,
        "events": [{
            "id": "reset-event-1", "type": "direct_reset", "label": "全员重置",
            "status": "announced", "title": "Tibo 预告将重置额度", "scope": "",
            "createdAt": "2026-09-22T12:31:32+08:00",
            "updatedAt": "2026-09-22T12:31:32+08:00", "confirmedAt": None,
            "occurredOn": None, "confirmationBasis": None, "schedule": None,
            "url": "https://aihot.news/codex-reset",
            "posts": [{
                "id": "2102254445082116335", "publishedAt": "2026-09-22T12:31:32+08:00",
                "stage": "预告", "text": "我承诺过周二重置。",
                "originalText": "I promised a reset for Tuesday",
                "url": "https://x.com/thsottiaux/status/2102254445082116335",
            }],
        }],
    }


class FakeResponse:
    def __init__(self, data: dict):
        self.data = data
        self.headers = {"ETag": '"snapshot-1"', "Cache-Control": "public, s-maxage=300"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.data).encode("utf-8")


class AihotParsingTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict("os.environ", {"SOURCE_MAX_AGE_HOURS": "6"})
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()

    def test_post_identity_and_original_excerpt_preserved(self):
        posts = sources._parse_aihot(snapshot(), now=NOW)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].key, "aihot:2102254445082116335")
        self.assertEqual(posts[0].source, "x")
        self.assertEqual(posts[0].url, snapshot()["events"][0]["posts"][0]["url"])
        self.assertIn("原文摘录：\nI promised a reset for Tuesday", posts[0].body)
        self.assertEqual(posts[0].created_at, "2026-09-22T12:31:32+08:00")

    def test_duplicates_and_receipt_status_changes_do_not_create_posts(self):
        data = snapshot()
        data["events"].append(copy.deepcopy(data["events"][0]))
        data["events"][1].update(id="regrouped-event", status="confirmed", confirmationBasis="receipt_review")
        data["count"] = 2
        self.assertEqual(len(sources._parse_aihot(data, now=NOW)), 1)

    def test_receipt_without_source_posts_creates_no_alert(self):
        data = snapshot()
        data["events"][0].update(posts=[], status="confirmed", confirmationBasis="receipt_review")
        self.assertEqual(sources._parse_aihot(data, now=NOW), [])

    def test_short_contextual_reply_is_preserved(self):
        data = snapshot()
        data["events"][0]["posts"][0].update(originalText="Yes", text="是的。")
        self.assertIn("Yes", sources._parse_aihot(data, now=NOW)[0].body)

    def test_other_authors_and_mismatched_ids_fail_closed(self):
        for changes in [
            {"url": "https://x.com/another/status/2102254445082116335"},
            {"url": "https://x.com/thsottiaux/status/999"},
            {"url": "https://x.com.evil.test/thsottiaux/status/2102254445082116335"},
            {"url": "https://x.com/thsottiaux/status/2102254445082116335?redirect=evil"},
            {"author": "another"},
        ]:
            with self.subTest(changes=changes):
                data = snapshot()
                data["events"][0]["posts"][0].update(changes)
                with self.assertRaises(ValueError):
                    sources._parse_aihot(data, now=NOW)

    def test_stale_scan_fails_but_old_post_with_fresh_scan_is_valid(self):
        data = snapshot()
        data["events"][0]["posts"][0]["publishedAt"] = "2026-06-15T12:00:00+08:00"
        self.assertEqual(len(sources._parse_aihot(data, now=NOW)), 1)
        data["checkedAt"] = (NOW - timedelta(hours=7)).isoformat()
        with self.assertRaisesRegex(ValueError, "stale"):
            sources._parse_aihot(data, now=NOW)

    def test_explicit_stale_and_missing_verification_fail(self):
        for changes in [{"stale": True}, {"checkedAt": None}, {"checkedAt": "2026-09-22T10:00:00"}]:
            with self.subTest(changes=changes):
                data = snapshot()
                data.update(changes)
                with self.assertRaises(ValueError):
                    sources._parse_aihot(data, now=NOW)

    def test_incompatible_schema_fails_instead_of_becoming_empty_success(self):
        for changes in [{"schemaVersion": 2}, {"schemaVersion": True}, {"events": {}}, {"count": 2}, {"count": True}]:
            with self.subTest(changes=changes):
                data = snapshot()
                data.update(changes)
                with self.assertRaises(ValueError):
                    sources._parse_aihot(data, now=NOW)

    def test_configurable_freshness_threshold_is_validated(self):
        for value in ["0", "-1", "nan", "inf", "bad"]:
            with self.subTest(value=value), patch.dict("os.environ", {"SOURCE_MAX_AGE_HOURS": value}):
                with self.assertRaisesRegex(ValueError, "SOURCE_MAX_AGE_HOURS"):
                    sources._parse_aihot(snapshot(), now=NOW)
        with patch.dict("os.environ", {"SOURCE_MAX_AGE_HOURS": "1"}):
            data = snapshot()
            data["checkedAt"] = (NOW - timedelta(hours=2)).isoformat()
            with self.assertRaisesRegex(ValueError, "stale"):
                sources._parse_aihot(data, now=NOW)


class AihotHttpTests(unittest.TestCase):
    def setUp(self):
        sources._AIHOT_CACHE.clear()
        self.cfg = Config(watch_changelog=False)
        self.data = snapshot()
        self.data["checkedAt"] = datetime.now(timezone.utc).isoformat()

    def tearDown(self):
        sources._AIHOT_CACHE.clear()

    def test_memory_cache_avoids_requests_inside_five_minutes(self):
        with patch.object(sources.urllib.request, "urlopen", return_value=FakeResponse(self.data)) as opened:
            first = sources.fetch_aihot_posts(self.cfg)
            second = sources.fetch_aihot_posts(self.cfg)
            self.assertEqual(first, second)
            self.assertEqual(opened.call_count, 1)

    def test_conditional_request_and_304_preserve_snapshot(self):
        with patch.object(sources.urllib.request, "urlopen", return_value=FakeResponse(self.data)):
            first = sources.fetch_aihot_posts(self.cfg)
        sources._AIHOT_CACHE[sources.AIHOT_URL]["fetched_at"] -= 601
        error = urllib.error.HTTPError(sources.AIHOT_URL, 304, "Not Modified", {}, None)
        with patch.object(sources.urllib.request, "urlopen", side_effect=error) as opened:
            second = sources.fetch_aihot_posts(self.cfg)
            self.assertEqual(first, second)
            self.assertEqual(opened.call_args.args[0].get_header("If-none-match"), '"snapshot-1"')

    def test_304_without_cache_is_explicit_failure(self):
        error = urllib.error.HTTPError(sources.AIHOT_URL, 304, "Not Modified", {}, None)
        with patch.object(sources.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "304 without"):
                sources.fetch_aihot_posts(self.cfg)

    def test_rate_limit_does_not_retry_before_retry_after(self):
        error = urllib.error.HTTPError(sources.AIHOT_URL, 429, "Too Many Requests", {"Retry-After": "120"}, None)
        with patch.object(sources.urllib.request, "urlopen", side_effect=error) as opened, patch.object(sources.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "Retry-After=120"):
                sources.fetch_aihot_posts(self.cfg)
            self.assertEqual(opened.call_count, 1)
            sleep.assert_not_called()

    def test_transient_error_uses_bounded_retries(self):
        with patch.object(sources.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")) as opened, patch.object(sources.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
                sources.fetch_aihot_posts(self.cfg)
            self.assertEqual(opened.call_count, 3)

    def test_required_feed_failure_is_not_hidden_by_changelog(self):
        self.cfg.watch_changelog = True
        with patch.object(sources, "fetch_aihot_posts", side_effect=ValueError("stale")), patch.object(sources, "fetch_changelog_entries") as changelog:
            with self.assertRaisesRegex(ValueError, "stale"):
                sources.collect(self.cfg)
            changelog.assert_not_called()


if __name__ == "__main__":
    unittest.main()

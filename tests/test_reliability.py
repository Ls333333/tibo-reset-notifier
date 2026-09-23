"""Acceptance checks for durable, private notification delivery.

All outbound transports are blocked; these tests send no real notifications.
Run with: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from watcher import notify, run
from watcher.config import Config
from watcher.model import Post
from watcher.state import StateStore


def post(number: int) -> Post:
    return Post(
        key=f"x:{number}",
        source="x",
        title=f"Codex limits reset {number}",
        body=f"We will reset all Codex rate limits. Announcement {number}.",
        url=f"https://x.com/thsottiaux/status/{number}",
        created_at="2026-09-22T01:00:00Z",
    )


class IsolatedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_path = Path(self.temp.name) / "watcher_state.json"
        for target in ("urllib.request.urlopen", "smtplib.SMTP", "smtplib.SMTP_SSL"):
            guard = patch(target, side_effect=AssertionError("Real network is forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def config(self, **overrides) -> Config:
        values = dict(state_file=self.state_path, watch_changelog=False,
                      ntfy_topic="test-only-topic", alert_all=True)
        values.update(overrides)
        return Config(**values)

    def cycle(self, cfg, posts, **kwargs):
        with patch("watcher.run.sources.collect", return_value=posts), \
                contextlib.redirect_stdout(io.StringIO()):
            return run.check_once(cfg, **kwargs)

    def baseline(self, cfg) -> None:
        with patch("watcher.run.notify.send_channel") as send:
            self.cycle(cfg, [post(0)])
        send.assert_not_called()


class DurableDeliveryTests(IsolatedTestCase):
    def test_first_run_records_baseline_without_sending_historical_alerts(self):
        cfg = self.config()
        with patch("watcher.run.notify.send_channel") as send:
            self.cycle(cfg, [post(1), post(2)])
        send.assert_not_called()
        self.assertTrue(self.state_path.is_file())
        self.assertTrue(StateStore(self.state_path).is_seen(post(1).key))
        with patch("watcher.run.notify.send_channel", return_value=(True, "sent")) as send:
            self.cycle(cfg, [post(1), post(2), post(3)])
        self.assertEqual([call.args[2].key for call in send.call_args_list], [post(3).key])

    def test_failed_post_retries_even_when_next_fetch_is_empty(self):
        cfg = self.config()
        self.baseline(cfg)
        with patch("watcher.run.notify.send_channel", return_value=(False, "temporary")) as send:
            self.cycle(cfg, [post(1)])
        self.assertEqual(send.call_count, 1)
        with patch("watcher.run.notify.send_channel", return_value=(True, "sent")) as send:
            self.cycle(cfg, [])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[2].key, post(1).key)
        with patch("watcher.run.notify.send_channel") as send:
            self.cycle(cfg, [post(1)])
        send.assert_not_called()

    def test_partial_delivery_retries_only_the_failed_channel(self):
        cfg = self.config(telegram_token="test-token", telegram_chat_id="test-chat")
        self.baseline(cfg)
        calls = []

        def partly_successful(cfg, channel, item, score_total, score_hits):
            calls.append((channel, item.key))
            return channel == "ntfy", "test response"

        with patch("watcher.run.notify.send_channel", side_effect=partly_successful):
            self.cycle(cfg, [post(1)])
        self.assertCountEqual(calls, [("ntfy", post(1).key), ("telegram", post(1).key)])
        with patch("watcher.run.notify.send_channel", return_value=(True, "sent")) as send:
            self.cycle(cfg, [])
        self.assertEqual([(c.args[1], c.args[2].key) for c in send.call_args_list],
                         [("telegram", post(1).key)])
        with patch("watcher.run.notify.send_channel") as send:
            self.cycle(cfg, [post(1)])
        send.assert_not_called()

    def test_cycle_cap_defers_remaining_posts_without_losing_them(self):
        cfg = self.config()
        self.baseline(cfg)
        items = [post(i) for i in range(1, 8)]
        with patch("watcher.run.notify.send_channel", return_value=(True, "sent")) as send:
            self.cycle(cfg, items)
        first_keys = [c.args[2].key for c in send.call_args_list]
        self.assertEqual(len(first_keys), 5)
        with patch("watcher.run.notify.send_channel", return_value=(True, "sent")) as send:
            self.cycle(cfg, [])
        second_keys = [c.args[2].key for c in send.call_args_list]
        self.assertEqual(len(second_keys), 2)
        self.assertCountEqual(first_keys + second_keys, [p.key for p in items])

    def test_dry_run_preserves_pending_state_and_does_not_send(self):
        cfg = self.config()
        self.baseline(cfg)
        with patch("watcher.run.notify.send_channel", return_value=(False, "temporary")):
            self.cycle(cfg, [post(1)])
        before = self.state_path.read_bytes()
        with patch("watcher.run.notify.send_channel") as send:
            self.cycle(cfg, [post(2)], dry_run=True)
        send.assert_not_called()
        self.assertEqual(self.state_path.read_bytes(), before)
        with patch("watcher.run.notify.send_channel", return_value=(True, "sent")) as send:
            self.cycle(cfg, [post(2)])
        self.assertCountEqual([c.args[2].key for c in send.call_args_list],
                              [post(1).key, post(2).key])

    def test_first_dry_run_does_not_create_state_file(self):
        cfg = self.config()
        with patch("watcher.run.notify.send_channel") as send:
            self.cycle(cfg, [post(1)], dry_run=True)
        send.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_prime_keeps_existing_pending_without_sending_or_queueing_history(self):
        cfg = self.config()
        self.baseline(cfg)
        with patch("watcher.run.notify.send_channel", return_value=(False, "temporary")):
            self.cycle(cfg, [post(1)])
        with patch("watcher.run.notify.send_channel") as send:
            self.cycle(cfg, [post(2)], prime=True)
        send.assert_not_called()
        with patch("watcher.run.notify.send_channel", return_value=(True, "sent")) as send:
            self.cycle(cfg, [post(2)])
        self.assertEqual([c.args[2].key for c in send.call_args_list], [post(1).key])

    def test_corrupt_state_is_rejected_and_never_overwritten(self):
        cfg = self.config()
        for data in (b"{broken-json", b"[]", b'{"version": 1, "seen": []}'):
            with self.subTest(data=data):
                self.state_path.write_bytes(data)
                with patch("watcher.run.notify.send_channel") as send:
                    with self.assertRaises((ValueError, RuntimeError, OSError)):
                        self.cycle(cfg, [post(1)])
                send.assert_not_called()
                self.assertEqual(self.state_path.read_bytes(), data)

    def test_string_state_path_is_accepted_across_cycles(self):
        cfg = self.config(state_file=str(self.state_path))
        self.baseline(cfg)
        with patch("watcher.run.notify.send_channel", return_value=(True, "sent")) as send:
            self.cycle(cfg, [post(1)])
        self.assertEqual(send.call_count, 1)
        self.assertTrue(StateStore(str(self.state_path)).is_seen(post(1).key))

    def test_source_failure_retries_saved_pending_and_still_reports_failure(self):
        cfg = self.config()
        self.baseline(cfg)
        with patch("watcher.run.notify.send_channel", return_value=(False, "temporary")):
            self.cycle(cfg, [post(1)])
        outage = RuntimeError("Required source is unavailable")
        with patch("watcher.run.sources.collect", side_effect=outage), \
                patch("watcher.run.notify.send_channel", return_value=(True, "sent")) as send, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError) as result:
                run.check_once(cfg)
        self.assertIs(result.exception, outage)
        self.assertEqual([c.args[2].key for c in send.call_args_list], [post(1).key])
        with patch("watcher.run.notify.send_channel") as send:
            self.cycle(cfg, [])
        send.assert_not_called()

    def test_aihot_contextual_reply_is_not_discarded_by_keyword_filter(self):
        cfg = self.config(alert_all=False)
        self.baseline(cfg)
        item = Post(key="aihot:123", source="x", title="Done!", body="Done!",
                    url="https://x.com/thsottiaux/status/123", created_at="2026-09-22T01:00:00Z",
                    is_reply=True)
        with patch("watcher.run.notify.send_channel", return_value=(True, "sent")) as send:
            self.cycle(cfg, [item])
        self.assertEqual([c.args[2].key for c in send.call_args_list], [item.key])


class NotificationContractTests(IsolatedTestCase):
    def pushplus_config(self, **overrides):
        return self.config(ntfy_topic="", pushplus_token="pushplus-test-secret",
                           pushplus_channel="wechat", **overrides)

    def test_pushplus_http_200_with_api_error_is_failure(self):
        cfg = self.pushplus_config()
        with patch("watcher.notify.http_post", return_value=(200, '{"code": 600, "msg": "error"}')):
            ok, note = notify.send_channel(cfg, "pushplus", post(1), 5, "limits-reset")
        self.assertFalse(ok)
        self.assertIsInstance(note, str)

    def test_pushplus_api_200_is_success_and_uses_requested_channel(self):
        cfg = self.pushplus_config()
        with patch("watcher.notify.http_post", return_value=(200, '{"code": 200, "data": "receipt"}')) as transport:
            ok, note = notify.send_channel(cfg, "pushplus", post(1), 5, "limits-reset")
        self.assertTrue(ok)
        self.assertIsInstance(note, str)
        payload = json.loads(transport.call_args.args[1])
        self.assertEqual(payload["token"], cfg.pushplus_token)
        self.assertEqual(payload["channel"], "wechat")
        self.assertIn("pushplus", cfg.active_channels())

    def test_pushplus_malformed_response_is_failure(self):
        cfg = self.pushplus_config()
        for response in ("not json", "[]", "null", "{}", '{"code": "200"}',
                         '{"code": false}', '{"code": true}'):
            with self.subTest(response=response), \
                    patch("watcher.notify.http_post", return_value=(200, response)):
                ok, _ = notify.send_channel(cfg, "pushplus", post(1), 5, "limits-reset")
                self.assertFalse(ok)

    def test_send_alert_and_send_test_keep_list_of_result_tuples(self):
        cfg = self.pushplus_config()
        with patch("watcher.notify.http_post", return_value=(200, '{"code": 200}')):
            result_lists = [notify.send_alert(cfg, post(1), 5, "limits-reset"),
                            notify.send_test(cfg)]
        for results in result_lists:
            self.assertIsInstance(results, list)
            self.assertEqual(len(results), 1)
            self.assertIsInstance(results[0], tuple)
            self.assertEqual(len(results[0]), 2)
            self.assertIs(results[0][0], True)
            self.assertIsInstance(results[0][1], str)

    def test_http_failure_logs_do_not_echo_credentials(self):
        secrets = ["private-pushplus-token", "private-telegram-token", "private-topic"]
        cfg = self.config(pushplus_token=secrets[0], telegram_token=secrets[1],
                          telegram_chat_id="test-chat", ntfy_topic=secrets[2])
        leaked_response = "server echoed " + " ".join(secrets)
        for channel in ("pushplus", "telegram", "ntfy"):
            with self.subTest(channel=channel), \
                    patch("watcher.notify.http_post", return_value=(500, leaked_response)):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    ok, note = notify.send_channel(cfg, channel, post(1), 5, "limits-reset")
                self.assertFalse(ok)
                for secret in secrets:
                    self.assertNotIn(secret, note + output.getvalue())

    def test_transport_exception_does_not_echo_token_or_escape(self):
        cfg = self.config(ntfy_topic="", telegram_token="private-telegram-token",
                          telegram_chat_id="test-chat")
        error = urllib.error.URLError("https://api.telegram.org/botprivate-telegram-token/sendMessage")
        with patch("watcher.notify.http_post", side_effect=error):
            ok, note = notify.send_channel(cfg, "telegram", post(1), 5, "limits-reset")
        self.assertFalse(ok)
        self.assertNotIn(cfg.telegram_token, note)

    def test_email_error_does_not_echo_smtp_password(self):
        cfg = self.config(ntfy_topic="", smtp_host="smtp.example.invalid", smtp_port=587,
                          smtp_user="test-user", smtp_password="private-smtp-password",
                          mail_to="recipient@example.invalid")
        with patch("watcher.notify.smtplib.SMTP", side_effect=smtplib.SMTPException(cfg.smtp_password)):
            ok, note = notify.send_channel(cfg, "email", post(1), 5, "limits-reset")
        self.assertFalse(ok)
        self.assertNotIn(cfg.smtp_password, note)


if __name__ == "__main__":
    unittest.main()

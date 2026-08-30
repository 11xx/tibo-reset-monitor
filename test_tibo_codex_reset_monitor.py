import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from tibo_codex_reset_monitor import (
    STATUS_URL,
    bar_payload,
    ResetSignal,
    Source,
    StatusSnapshot,
    Watch,
    classify_reset,
    fetch_status,
    forecast,
    format_local_time,
    format_watch_alert,
    parse_status,
    run,
    watch_signature,
)


def reset_payload(
    *,
    reset_id="reset-1",
    reset_type="regular",
    text="I have reset usage limits for all paid users of ChatGPT Work and Codex.",
    source_type="x_post",
):
    return {
        "id": reset_id,
        "reset_type": reset_type,
        "announced_at": "2026-08-26T12:00:00Z",
        "text": text,
        "source": {
            "type": source_type,
            "author": "thsottiaux" if source_type == "x_post" else None,
            "url": "https://x.com/thsottiaux/status/1",
        },
    }


def status_payload(*, reset=None, watch=None):
    return json.dumps(
        {
            "data": {
                "latest_reset": reset,
                "active_watch": watch,
                "stats": {
                    "total": 46,
                    "last_reset_at": "2026-08-26T12:00:00Z",
                    "days_since_last": 0.5,
                    "avg_interval_days": 7.6,
                },
            },
            "meta": {"api_version": "v1", "generated_at": "2026-08-26T12:00:00Z"},
        }
    ).encode()


NOW = datetime(2026, 8, 30, 20, 0, tzinfo=timezone.utc)


class FakeResponse:
    status = 200

    def __init__(self, payload, etag='"etag-1"'):
        self.payload = payload
        self.headers = {"ETag": etag}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.payload


class MonitorTests(unittest.TestCase):
    def test_parse_status_and_prediction(self):
        watch = {
            "level": "strong",
            "reset_chance_percent": 80,
            "forecast_window": "within 24 hours",
            "observed_at": "2026-08-26T12:00:00Z",
            "expires_at": "2026-08-27T12:00:00Z",
            "text": "More resets coming tomorrow for Plus and Pro.",
            "source": {"type": "x_post", "author": "thsottiaux", "url": "https://x.com/1"},
        }
        snapshot = parse_status(status_payload(reset=reset_payload(), watch=watch))
        self.assertEqual(snapshot.latest_reset.id, "reset-1")
        self.assertEqual(snapshot.active_watch.level, "strong")
        self.assertEqual(snapshot.active_watch.reset_chance_percent, 80)
        self.assertEqual(snapshot.stats["total"], 46)
        event, action, body = format_watch_alert(snapshot.active_watch, snapshot.stats)
        self.assertEqual(event, "FOLLOW-UP TEASED")
        self.assertEqual(action, "SPEND NOW")
        self.assertIn("Forecast: strong; chance 80%; window within 24 hours", body)

    def test_rejects_malformed_status(self):
        with self.assertRaisesRegex(ValueError, "data.latest_reset.id"):
            parse_status(status_payload(reset={"reset_type": "regular"}))

    def test_fetch_uses_etag_and_accepts_not_modified(self):
        with patch("tibo_codex_reset_monitor.urllib.request.urlopen") as urlopen:
            urlopen.return_value = FakeResponse(status_payload(reset=reset_payload()))
            snapshot, etag = fetch_status('"old-etag"')
            request = urlopen.call_args.args[0]
            self.assertEqual(request.get_header("If-none-match"), '"old-etag"')
            self.assertEqual(snapshot.latest_reset.id, "reset-1")
            self.assertEqual(etag, '"etag-1"')

            urlopen.side_effect = HTTPError(STATUS_URL, 304, "Not Modified", {}, None)
            snapshot, etag = fetch_status('"etag-1"')
            self.assertIsNone(snapshot)
            self.assertEqual(etag, '"etag-1"')

    def test_classifies_signal_shapes(self):
        self.assertEqual(
            classify_reset(
                ResetSignal(
                    "1",
                    "banked",
                    "2026-08-26T12:00:00Z",
                    "A banked reset is credited.",
                    Source("x_post"),
                )
            ),
            "BANKED RESET CREDITED",
        )
        self.assertEqual(
            classify_reset(
                ResetSignal(
                    "2",
                    "regular",
                    "2026-08-26T12:00:00Z",
                    "The reset has propagated after a fix.",
                    Source("x_post"),
                )
            ),
            "BUG/FIX RESET",
        )
        self.assertEqual(
            classify_reset(
                ResetSignal(
                    "3",
                    "regular",
                    "2026-08-26T12:00:00Z",
                    "A reset will happen tomorrow.",
                    Source("x_post"),
                )
            ),
            "FOLLOW-UP TEASED",
        )
        self.assertEqual(
            classify_reset(
                ResetSignal(
                    "4",
                    "regular",
                    "2026-08-26T12:00:00Z",
                    "@someone Ah yeah, forgot to say",
                    Source("observed"),
                )
            ),
            "SILENT RESET REPORT",
        )

    def test_local_time_is_sao_paulo(self):
        self.assertEqual(format_local_time("2026-08-26T12:00:00Z"), "2026-08-26 09:00 (São Paulo)")

    def test_watch_signature_ignores_small_chance_changes(self):
        def watch(chance):
            return Watch(
                "strong",
                chance,
                "within 24 hours",
                "2026-08-26T12:00:00Z",
                "2026-08-27T12:00:00Z",
                "A reset may come soon.",
                Source("observed", "https://codex-resets.com/"),
            )

        self.assertEqual(watch_signature(watch(60)), watch_signature(watch(70)))
        self.assertNotEqual(watch_signature(watch(60)), watch_signature(watch(80)))

    def test_migration_seeds_without_notifying(self):
        snapshot = StatusSnapshot(
            ResetSignal(
                "reset-1",
                "regular",
                "2026-08-26T12:00:00Z",
                "I have reset Codex usage limits.",
                Source("x_post", "https://x.com/1"),
            ),
            None,
            {"total": 46},
        )
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text(json.dumps({"initialized": True, "seen": ["old"]}))
            with (
                patch("tibo_codex_reset_monitor.state_path", return_value=state_file),
                patch("tibo_codex_reset_monitor.fetch_status", return_value=(snapshot, '"etag"')),
                patch("tibo_codex_reset_monitor.notify") as notify,
            ):
                self.assertEqual(run(), 0)
            state = json.loads(state_file.read_text())
            self.assertTrue(state["api_initialized"])
            self.assertEqual(state["last_reset_id"], "reset-1")
            notify.assert_not_called()

    def test_new_reset_notifies_once_with_rich_context(self):
        snapshot = StatusSnapshot(
            ResetSignal(
                "reset-2",
                "regular",
                "2026-08-26T12:00:00Z",
                "I have reset usage limits for all paid users of ChatGPT Work and Codex.",
                Source("x_post", "https://x.com/2"),
            ),
            None,
            {"total": 47, "days_since_last": 0, "avg_interval_days": 7.6},
        )
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "api_initialized": True,
                        "last_reset_id": "reset-1",
                        "watch_signature": None,
                    }
                )
            )
            with (
                patch("tibo_codex_reset_monitor.state_path", return_value=state_file),
                patch("tibo_codex_reset_monitor.fetch_status", return_value=(snapshot, '"etag"')),
                patch("tibo_codex_reset_monitor.notify") as notify,
            ):
                self.assertEqual(run(), 0)
                summary, body = notify.call_args.args[:2]
                self.assertIn("RESET PROPAGATED", summary)
                self.assertIn("Scope: Codex + ChatGPT Work", body)
                self.assertIn("Affected plans: all paid plans (stated)", body)
                self.assertIn("Time (São Paulo): 2026-08-26 09:00", body)

                notify.reset_mock()
                self.assertEqual(run(), 0)
                notify.assert_not_called()


    def test_forecast_prefers_a_live_watch_over_cadence(self):
        watch = Watch(
            "strong",
            88,
            "by 6pm",
            "2026-08-30T19:00:00Z",
            "2026-08-31T01:00:00Z",
            "Your reset lands at 6pm.",
            Source("x_post", "https://x.com/1"),
        )
        snapshot = StatusSnapshot(
            ResetSignal(
                "reset-1",
                "regular",
                "2026-08-25T12:00:00Z",
                "Usage reset for Codex.",
                Source("x_post"),
            ),
            watch,
            {"days_since_last": 5, "avg_interval_days": 7.4},
        )
        prediction = forecast(snapshot, NOW)
        self.assertEqual((prediction.probability, prediction.basis), (88, "watch"))
        self.assertEqual((prediction.stage, prediction.action), ("spend-now", "SPEND NOW"))

    def test_forecast_reads_certainty_from_a_reset_just_announced(self):
        snapshot = StatusSnapshot(
            ResetSignal(
                "reset-2",
                "regular",
                "2026-08-30T19:30:00Z",
                "Usage reset for Codex.",
                Source("x_post"),
            ),
            None,
            {"days_since_last": 0, "avg_interval_days": 7.4},
        )
        prediction = forecast(snapshot, NOW)
        self.assertEqual((prediction.probability, prediction.basis), (100, "reset"))
        self.assertEqual(prediction.stage, "landed")

    def test_forecast_drops_a_watch_its_own_reset_has_overtaken(self):
        watch = Watch(
            "strong",
            88,
            "by 6pm",
            "2026-08-29T19:00:00Z",
            "2026-08-31T01:00:00Z",
            "A reset is coming.",
            Source("x_post"),
        )
        snapshot = StatusSnapshot(
            ResetSignal(
                "reset-3",
                "regular",
                "2026-08-30T10:00:00Z",
                "Usage reset for Codex.",
                Source("x_post"),
            ),
            watch,
            {"days_since_last": 0.4, "avg_interval_days": 7.4},
        )
        prediction = forecast(snapshot, NOW)
        self.assertEqual(prediction.basis, "cadence")
        self.assertEqual(prediction.stage, "wait")

    def test_forecast_expires_a_watch_on_its_own_deadline(self):
        watch = Watch(
            "strong",
            88,
            "by 6pm",
            "2026-08-29T19:00:00Z",
            "2026-08-30T18:00:00Z",
            "A reset is coming.",
            Source("x_post"),
        )
        snapshot = StatusSnapshot(None, watch, {"days_since_last": 7.4, "avg_interval_days": 7.4})
        prediction = forecast(snapshot, NOW)
        self.assertEqual(prediction.basis, "cadence")

    def test_cadence_alone_never_reaches_the_actionable_threshold(self):
        snapshot = StatusSnapshot(None, None, {"days_since_last": 40, "avg_interval_days": 7.4})
        prediction = forecast(snapshot, NOW)
        self.assertEqual(prediction.probability, 45)
        self.assertEqual((prediction.stage, prediction.action), ("wait", "WAIT"))

    def test_bar_payload_hides_a_quiet_forecast_and_shows_an_actionable_one(self):
        quiet = StatusSnapshot(None, None, {"days_since_last": 1, "avg_interval_days": 7.4})
        payload = bar_payload({"snapshot": quiet.raw, "fetched_at": "2026-08-30T19:58:00Z"}, NOW)
        self.assertFalse(payload["visible"])

        watch = {
            "level": "elevated",
            "reset_chance_percent": 60,
            "forecast_window": "within 24 hours",
            "observed_at": "2026-08-30T18:00:00Z",
            "expires_at": "2026-08-31T12:00:00Z",
            "text": "A reset may land later today.",
            "source": {"type": "observed", "author": None, "url": None},
        }
        snapshot = parse_status(status_payload(reset=reset_payload(), watch=watch))
        payload = bar_payload(
            {"snapshot": snapshot.raw, "fetched_at": "2026-08-30T19:58:00Z"}, NOW
        )
        self.assertTrue(payload["visible"])
        self.assertEqual(payload["state"], "fresh")
        self.assertEqual(payload["probability"], 60)
        self.assertEqual(payload["action"], "USE SOME")
        self.assertIn("Codex reset chance 60%", payload["tooltip"])
        self.assertIn("window within 24 hours", payload["tooltip"])

    def test_bar_payload_marks_a_cache_the_monitor_stopped_refreshing(self):
        snapshot = parse_status(status_payload(reset=reset_payload()))
        payload = bar_payload(
            {"snapshot": snapshot.raw, "fetched_at": "2026-08-30T18:00:00Z"}, NOW
        )
        self.assertEqual(payload["state"], "stale")
        self.assertIn("2h 00m ago", payload["tooltip"])

    def test_bar_payload_without_a_cache_shows_nothing(self):
        payload = bar_payload({}, NOW)
        self.assertEqual(payload["state"], "unavailable")
        self.assertFalse(payload["visible"])

    def test_growth_inside_a_chance_band_still_notifies_once_escalated(self):
        def snapshot_at(chance):
            return parse_status(
                status_payload(
                    reset=reset_payload(),
                    watch={
                        "level": "elevated",
                        "reset_chance_percent": chance,
                        "forecast_window": "within 24 hours",
                        "observed_at": "2026-08-30T18:00:00Z",
                        "expires_at": "2026-08-31T12:00:00Z",
                        "text": "A reset may land later today.",
                        "source": {"type": "observed", "author": None, "url": None},
                    },
                )
            )

        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "api_initialized": True,
                        "last_reset_id": "reset-1",
                        "watch_signature": watch_signature(snapshot_at(51).active_watch),
                        "stage": "use-some",
                    }
                )
            )
            with (
                patch("tibo_codex_reset_monitor.state_path", return_value=state_file),
                patch(
                    "tibo_codex_reset_monitor.fetch_status",
                    return_value=(snapshot_at(74), '"etag"'),
                ),
                patch("tibo_codex_reset_monitor.notify") as notify,
            ):
                self.assertEqual(run(), 0)
                notify.assert_not_called()
            state = json.loads(state_file.read_text())
            self.assertEqual(state["stage"], "use-some")
            self.assertEqual(state["snapshot"]["active_watch"]["reset_chance_percent"], 74)

            with (
                patch("tibo_codex_reset_monitor.state_path", return_value=state_file),
                patch(
                    "tibo_codex_reset_monitor.fetch_status",
                    return_value=(snapshot_at(76), '"etag"'),
                ),
                patch("tibo_codex_reset_monitor.notify") as notify,
            ):
                self.assertEqual(run(), 0)
                summary, _body = notify.call_args.args[:2]
                self.assertIn("chance 76%", notify.call_args.args[1])
            self.assertEqual(json.loads(state_file.read_text())["stage"], "spend-now")


if __name__ == "__main__":
    unittest.main()

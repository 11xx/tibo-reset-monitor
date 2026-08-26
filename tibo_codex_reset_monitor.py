#!/usr/bin/env python3
"""Notify when Codex Resets reports a Tibo Codex usage-limit signal."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APP = "tibo-codex-reset-monitor"
STATUS_URL = "https://codex-resets.com/api/v1/status"
TRACKER_URL = "https://codex-resets.com/"
USER_AGENT = f"{APP}/2.0"
MAX_RESPONSE_BYTES = 512 * 1024

SPACE_RE = re.compile(r"\s+")
POLICY_RE = re.compile(
    r"(?:\bpolicy\b|\b5[- ]?hour\b|\brolling\b.{0,30}\bwindow\b|"
    r"\b(?:re-?add|restore|bring back)\b.{0,40}\b(?:limit|window)\b)",
    re.IGNORECASE,
)
BUG_RE = re.compile(
    r"\b(?:bug|issue|incident|fix(?:ed|es)?|mitigat(?:e|ed|ion)|"
    r"inefficien(?:cy|cies)|drain(?:ed|ing)?)\b",
    re.IGNORECASE,
)
COMPLETED_RE = re.compile(
    r"(?:\b(?:have|has|just|now)\b.{0,80}\breset\b|"
    r"\breset\b.{0,80}\b(?:complete|completed|done|propagated|landed|applied)\b)",
    re.IGNORECASE,
)
TEASER_RE = re.compile(
    r"\b(?:will|going to|plan(?:ning)? to|later|tonight|tomorrow|soon|"
    r"next|coming|around)\b",
    re.IGNORECASE,
)
IMMINENT_RE = re.compile(
    r"\b(?:now|today|tonight|tomorrow|soon|next hour|coming)\b|"
    r"\bin the next\b",
    re.IGNORECASE,
)

try:
    LOCAL_ZONE = ZoneInfo("America/Sao_Paulo")
except ZoneInfoNotFoundError:
    LOCAL_ZONE = timezone.utc


@dataclass(frozen=True)
class Source:
    type: str
    url: str | None = None
    author: str | None = None


@dataclass(frozen=True)
class ResetSignal:
    id: str
    reset_type: str
    announced_at: str
    text: str
    source: Source


@dataclass(frozen=True)
class Watch:
    level: str
    reset_chance_percent: int | None
    forecast_window: str
    observed_at: str
    expires_at: str
    text: str
    source: Source


@dataclass(frozen=True)
class StatusSnapshot:
    latest_reset: ResetSignal | None
    active_watch: Watch | None
    stats: dict[str, object]


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _content_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value.strip()


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, label)


def _parse_source(value: object, label: str) -> Source:
    raw = _mapping(value, label)
    source_type = raw.get("type")
    if source_type not in {"x_post", "observed"}:
        raise ValueError(f"{label}.type is not supported")
    author = _optional_string(raw.get("author"), f"{label}.author")
    url = _optional_string(raw.get("url"), f"{label}.url")
    return Source(source_type, url, author)


def _parse_reset(value: object) -> ResetSignal:
    raw = _mapping(value, "data.latest_reset")
    reset_type = raw.get("reset_type")
    if reset_type not in {"regular", "banked"}:
        raise ValueError("data.latest_reset.reset_type is not supported")
    return ResetSignal(
        id=_required_string(raw.get("id"), "data.latest_reset.id"),
        reset_type=reset_type,
        announced_at=_required_string(
            raw.get("announced_at"), "data.latest_reset.announced_at"
        ),
        text=_content_string(raw.get("text"), "data.latest_reset.text"),
        source=_parse_source(raw.get("source"), "data.latest_reset.source"),
    )


def _parse_watch(value: object) -> Watch:
    raw = _mapping(value, "data.active_watch")
    level = raw.get("level")
    if level not in {"elevated", "strong"}:
        raise ValueError("data.active_watch.level is not supported")
    chance = raw.get("reset_chance_percent")
    if chance is not None and (
        isinstance(chance, bool) or not isinstance(chance, int) or not 0 <= chance <= 100
    ):
        raise ValueError("data.active_watch.reset_chance_percent is invalid")
    return Watch(
        level=level,
        reset_chance_percent=chance,
        forecast_window=_required_string(
            raw.get("forecast_window"), "data.active_watch.forecast_window"
        ),
        observed_at=_required_string(
            raw.get("observed_at"), "data.active_watch.observed_at"
        ),
        expires_at=_required_string(
            raw.get("expires_at"), "data.active_watch.expires_at"
        ),
        text=_content_string(raw.get("text"), "data.active_watch.text"),
        source=_parse_source(raw.get("source"), "data.active_watch.source"),
    )


def parse_status(payload: bytes) -> StatusSnapshot:
    try:
        root = _mapping(json.loads(payload), "response")
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("status response is not valid JSON") from error

    data = _mapping(root.get("data"), "data")
    for field in ("latest_reset", "active_watch", "stats"):
        if field not in data:
            raise ValueError(f"data.{field} is missing")
    latest_value = data["latest_reset"]
    watch_value = data["active_watch"]
    return StatusSnapshot(
        latest_reset=None if latest_value is None else _parse_reset(latest_value),
        active_watch=None if watch_value is None else _parse_watch(watch_value),
        stats=_mapping(data["stats"], "data.stats"),
    )


def fetch_status(etag: str | None = None) -> tuple[StatusSnapshot | None, str | None]:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    request = urllib.request.Request(STATUS_URL, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if getattr(response, "status", 200) != 200:
                raise ValueError(f"status API returned HTTP {response.status}")
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ValueError("status response is too large")
            return parse_status(payload), response.headers.get("ETag") or etag
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return None, etag
        raise


def display_text(value: str, limit: int = 800) -> str:
    return SPACE_RE.sub(" ", value).strip()[:limit]


def format_local_time(value: str | None) -> str:
    if not value:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local = parsed.astimezone(LOCAL_ZONE)
    except ValueError:
        return value
    return f"{local:%Y-%m-%d %H:%M} (São Paulo)"


def describe_scope(text: str) -> str:
    lower = text.lower()
    scope = []
    if re.search(r"\bcodex\b", lower):
        scope.append("Codex")
    if re.search(r"\bchatgpt\s+work\b|\bwork\s+users?\b", lower):
        scope.append("ChatGPT Work")
    return " + ".join(scope) if scope else "unknown"


def describe_plans(text: str) -> str:
    lower = text.lower()
    plans = []
    if re.search(r"\ball\s+paid\b|\bpaid\s+(?:users?|plans?)\b", lower):
        plans.append("all paid plans (stated)")
    if re.search(r"\bplus\b", lower):
        plans.append("Plus")
    if re.search(r"\bpro\b", lower):
        plans.append("Pro")
    if re.search(r"\bbusiness\b", lower):
        plans.append("Business")
    if re.search(r"\benterprise\b", lower):
        plans.append("Enterprise")
    if re.search(r"\beveryone\b|\ball\s+users?\b", lower):
        plans.append("all users (stated)")
    return "; ".join(plans) if plans else "unknown"


def describe_reset_quota(reset: ResetSignal) -> str:
    lower = reset.text.lower()
    if reset.reset_type == "banked":
        return "banked reset"
    if re.search(r"\bpurchased\b|\bpaid credits?\b|\bcredit balance\b", lower):
        return "purchased credits (stated)"
    return "regular usage limit; included vs purchased unknown"


def classify_reset(reset: ResetSignal) -> str:
    if reset.reset_type == "banked":
        return "BANKED RESET CREDITED"
    if POLICY_RE.search(reset.text):
        return "LIMIT POLICY CHANGE"
    if BUG_RE.search(reset.text):
        return "BUG/FIX RESET"
    if COMPLETED_RE.search(reset.text):
        return "RESET PROPAGATED"
    if TEASER_RE.search(reset.text):
        return "FOLLOW-UP TEASED"
    if reset.source.type == "x_post":
        return "RESET ANNOUNCED"
    return "SILENT RESET REPORT"


def reset_action(reset: ResetSignal, event: str) -> str:
    if event == "BANKED RESET CREDITED":
        return "CHECK STATUS"
    if event in {"RESET ANNOUNCED", "FOLLOW-UP TEASED"}:
        return "SPEND NOW" if IMMINENT_RE.search(reset.text) else "USE SOME"
    return "CHECK STATUS"


def chance_band(chance: int | None) -> str:
    if chance is None:
        return "unknown"
    if chance >= 75:
        return "75-100%"
    if chance >= 50:
        return "50-74%"
    if chance >= 25:
        return "25-49%"
    return "0-24%"


def classify_watch(watch: Watch) -> str:
    if POLICY_RE.search(watch.text):
        return "LIMIT POLICY CHANGE"
    if BUG_RE.search(watch.text):
        return "BUG/FIX RESET"
    if TEASER_RE.search(watch.text):
        return "FOLLOW-UP TEASED"
    return "RESET WATCH UPDATED"


def watch_action(watch: Watch) -> str:
    if watch.reset_chance_percent is not None and watch.reset_chance_percent >= 75:
        return "SPEND NOW"
    if watch.level == "strong" or (
        watch.reset_chance_percent is not None and watch.reset_chance_percent >= 50
    ):
        return "USE SOME"
    if watch.reset_chance_percent is not None:
        return "WAIT"
    return "CHECK STATUS"


def watch_signature(watch: Watch | None) -> str | None:
    if watch is None:
        return None
    stable = {
        "level": watch.level,
        "chance_band": chance_band(watch.reset_chance_percent),
        "forecast_window": watch.forecast_window,
        "text": display_text(watch.text),
        "source": {"type": watch.source.type, "url": watch.source.url},
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def source_link(source: Source) -> str:
    return source.url or TRACKER_URL


def format_stats(stats: dict[str, object]) -> str:
    values = []
    total = stats.get("total")
    if isinstance(total, int) and not isinstance(total, bool):
        values.append(f"{total} tracked")
    days = stats.get("days_since_last")
    if isinstance(days, (int, float)) and not isinstance(days, bool):
        values.append(f"{days:g} days since last")
    average = stats.get("avg_interval_days")
    if isinstance(average, (int, float)) and not isinstance(average, bool):
        values.append(f"{average:g}-day average")
    return "; ".join(values) if values else "unknown"


def format_reset_alert(reset: ResetSignal, stats: dict[str, object]) -> tuple[str, str, str]:
    event = classify_reset(reset)
    action = reset_action(reset, event)
    body = "\n".join(
        [
            f"Event: {event}",
            f"Action: {action}",
            f"Time (São Paulo): {format_local_time(reset.announced_at)}",
            f"Scope: {describe_scope(reset.text)}",
            f"Affected plans: {describe_plans(reset.text)}",
            f"Quota: {describe_reset_quota(reset)}",
            f"Text: {display_text(reset.text)}",
            f"History: {format_stats(stats)}",
            f"Source post: {source_link(reset.source)}",
            f"Tracker: {TRACKER_URL} (unofficial; verify in Codex status)",
        ]
    )
    return event, action, body


def format_watch_alert(watch: Watch, stats: dict[str, object]) -> tuple[str, str, str]:
    event = classify_watch(watch)
    action = watch_action(watch)
    chance = (
        f"{watch.reset_chance_percent}%"
        if watch.reset_chance_percent is not None
        else "unknown"
    )
    body = "\n".join(
        [
            f"Event: {event}",
            f"Action: {action}",
            f"Forecast: {watch.level}; chance {chance}; window {watch.forecast_window}",
            f"Observed (São Paulo): {format_local_time(watch.observed_at)}",
            f"Expires (São Paulo): {format_local_time(watch.expires_at)}",
            f"Scope: {describe_scope(watch.text)}",
            f"Affected plans: {describe_plans(watch.text)}",
            "Quota: unknown (public forecast)",
            f"Text: {display_text(watch.text)}",
            f"History: {format_stats(stats)}",
            f"Source: {source_link(watch.source)}",
            f"Tracker: {TRACKER_URL} (unofficial; verify in Codex status)",
        ]
    )
    return event, action, body


def format_watch_cleared() -> tuple[str, str, str]:
    return (
        "RESET WATCH CLEARED",
        "WAIT",
        "\n".join(
            [
                "Event: RESET WATCH CLEARED",
                "Action: WAIT",
                "Forecast: no active forecast is currently published",
                "Scope: unknown",
                "Affected plans: unknown",
                "Quota: unknown",
                f"Tracker: {TRACKER_URL} (unofficial; verify in Codex status)",
            ]
        ),
    )


def format_status_report(snapshot: StatusSnapshot) -> str:
    lines = ["Codex Resets status", f"Tracker: {TRACKER_URL} (unofficial)"]
    if snapshot.latest_reset is None:
        lines.append("Latest reset: none")
    else:
        event, action, body = format_reset_alert(snapshot.latest_reset, snapshot.stats)
        lines.extend([f"Latest reset: {event} [{action}]", body])
    if snapshot.active_watch is None:
        lines.append("Active watch: none")
    else:
        event, action, body = format_watch_alert(snapshot.active_watch, snapshot.stats)
        lines.extend([f"Active watch: {event} [{action}]", body])
    return "\n".join(lines)


def state_path() -> Path:
    if directory := os.environ.get("STATE_DIRECTORY"):
        return Path(directory) / "state.json"
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return root / APP / "state.json"


def load_state() -> dict[str, object]:
    try:
        state = json.loads(state_path().read_text())
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def save_state(state: dict[str, object]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_failure(state: dict[str, object], error: Exception) -> int:
    failures = state.get("failures", 0)
    failures = failures if isinstance(failures, int) and not isinstance(failures, bool) else 0
    failures += 1
    state["failures"] = failures
    state["last_checked_at"] = now_utc()
    save_state(state)
    print(f"status failed: {error}", file=sys.stderr)
    if failures == 12 or (failures > 12 and failures % 288 == 0):
        notify(
            "Codex reset monitor is offline",
            "The public Codex Resets status API has failed repeatedly. Check the user service journal.",
            critical=True,
        )
    return 1


def run(show_latest: bool = False) -> int:
    state = load_state()
    saved_etag = state.get("etag")
    etag = saved_etag if isinstance(saved_etag, str) and not show_latest else None
    try:
        snapshot, response_etag = fetch_status(etag)
    except Exception as error:
        return record_failure(state, error)

    if snapshot is None:
        state["failures"] = 0
        state["last_checked_at"] = now_utc()
        save_state(state)
        print("status unchanged (304)")
        return 0

    if show_latest:
        print(format_status_report(snapshot))
        return 0

    current_reset_id = snapshot.latest_reset.id if snapshot.latest_reset else None
    current_watch_signature = watch_signature(snapshot.active_watch)
    if not state.get("api_initialized"):
        state.pop("initialized", None)
        state.pop("seen", None)
        state.update(
            {
                "api_initialized": True,
                "failures": 0,
                "last_checked_at": now_utc(),
                "last_reset_id": current_reset_id,
                "watch_signature": current_watch_signature,
            }
        )
        if response_etag:
            state["etag"] = response_etag
        save_state(state)
        print("seeded current Codex Resets status")
        return 0

    alerts: list[tuple[str, str, str]] = []
    if current_reset_id and current_reset_id != state.get("last_reset_id"):
        alerts.append(format_reset_alert(snapshot.latest_reset, snapshot.stats))

    if "watch_signature" in state and current_watch_signature != state["watch_signature"]:
        if snapshot.active_watch is None:
            alerts.append(format_watch_cleared())
        else:
            alerts.append(format_watch_alert(snapshot.active_watch, snapshot.stats))

    if alerts:
        labels = ", ".join(alert[0] for alert in alerts)
        body = "\n\n".join(alert[2] for alert in alerts)
        critical = any(alert[1] in {"SPEND NOW", "USE SOME"} for alert in alerts)
        notify(f"Codex reset monitor: {labels}", body[:4000], critical=critical)
        print(f"notified: {labels}")

    state.update(
        {
            "api_initialized": True,
            "failures": 0,
            "last_checked_at": now_utc(),
            "last_reset_id": current_reset_id,
            "watch_signature": current_watch_signature,
        }
    )
    if response_etag:
        state["etag"] = response_etag
    save_state(state)
    return 0


def notify(summary: str, body: str, critical: bool = False) -> None:
    command = shutil.which("notify-send")
    if not command:
        raise RuntimeError("notify-send is not installed")
    subprocess.run(
        [
            command,
            "--app-name=Codex reset monitor",
            f"--urgency={'critical' if critical else 'normal'}",
            f"--expire-time={0 if critical else 15000}",
            summary,
            body,
        ],
        check=True,
        timeout=10,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show-latest", action="store_true")
    parser.add_argument("--test-notification", action="store_true")
    args = parser.parse_args()
    if args.test_notification:
        notify("Codex reset monitor", "Notifications are working.", critical=True)
        return 0
    return run(args.show_latest)


if __name__ == "__main__":
    raise SystemExit(main())

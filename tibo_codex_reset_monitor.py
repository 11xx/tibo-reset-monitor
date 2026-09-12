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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APP = "tibo-codex-reset-monitor"
STATUS_URL = "https://codex-resets.com/api/v1/status"
TRACKER_URL = "https://codex-resets.com/"
USER_AGENT = f"{APP}/3.0"
MAX_RESPONSE_BYTES = 512 * 1024

# A reset stays worth spending on for a few hours after it is announced, so the
# forecast reports certainty rather than a prediction during that window.
LANDED_RESET_HOURS = 3
# The tracker publishes a scheduled reset without a probability, so the monitor
# supplies its own reading: an explicit announcement outranks cadence, and it
# crosses into SPEND NOW as the landing time arrives.
SCHEDULE_AHEAD_PERCENT = 65
SCHEDULE_IMMINENT_PERCENT = 80
SCHEDULE_IMMINENT_MINUTES = 60
# A schedule whose landing time has passed is awaiting evidence, not a dead
# letter: the tracker keeps it until it observes the reset.
SCHEDULE_DUE_PERCENT = 85
# A forecast published without a percentage still has a level; these are the
# percentages the two levels stand for.
LEVEL_CHANCE = {"strong": 75, "elevated": 45}
# Cadence alone is weak evidence: a long gap since the last reset raises the
# reading but never past this, which keeps it below the actionable threshold.
CADENCE_CEILING = 45
SPEND_NOW_PERCENT = 75
USE_SOME_PERCENT = 50
# How long a cached status stays worth showing without a hedge.
CACHE_FRESH_SECONDS = 15 * 60
# A reset that landed and a forecast worth spending now are both the top of the
# ladder: neither escalates into the other, and the reset alert already speaks.
STAGE_RANK = {"wait": 0, "use-some": 1, "spend-now": 2, "landed": 2}

BASIS_LABEL = {
    "reset": "a reset was just announced",
    "scheduled": "a reset is scheduled to land",
    "scheduled-due": "a scheduled reset is due, awaiting confirmation",
    "watch": "published forecast",
    "cadence": "reset cadence only, no live forecast",
    "unknown": "no forecast and no cadence published",
}

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
class ScheduledReset:
    id: str
    status: str
    reset_type: str
    announced_at: str
    scheduled_for: str | None
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
    scheduled_reset: ScheduledReset | None
    active_watch: Watch | None
    stats: dict[str, object]
    # The `data` object the snapshot was parsed from, kept so a cached status
    # can be restored through the same validation that accepted it.
    raw: dict[str, object] | None = None


@dataclass(frozen=True)
class Forecast:
    probability: int
    basis: str
    stage: str
    action: str


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


def _parse_scheduled_reset(value: object) -> ScheduledReset:
    raw = _mapping(value, "data.scheduled_reset")
    reset_type = raw.get("reset_type")
    if reset_type not in {"regular", "banked"}:
        raise ValueError("data.scheduled_reset.reset_type is not supported")
    return ScheduledReset(
        id=_required_string(raw.get("id"), "data.scheduled_reset.id"),
        status=_required_string(raw.get("status"), "data.scheduled_reset.status"),
        reset_type=reset_type,
        announced_at=_required_string(
            raw.get("announced_at"), "data.scheduled_reset.announced_at"
        ),
        scheduled_for=_optional_string(
            raw.get("scheduled_for"), "data.scheduled_reset.scheduled_for"
        ),
        text=_content_string(raw.get("text"), "data.scheduled_reset.text"),
        source=_parse_source(raw.get("source"), "data.scheduled_reset.source"),
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
    # `scheduled_reset` is required by the current API spec but absent from
    # responses written before it existed, so an absent field is not news here.
    scheduled_value = data.get("scheduled_reset")
    watch_value = data["active_watch"]
    return StatusSnapshot(
        latest_reset=None if latest_value is None else _parse_reset(latest_value),
        scheduled_reset=None
        if scheduled_value is None
        else _parse_scheduled_reset(scheduled_value),
        active_watch=None if watch_value is None else _parse_watch(watch_value),
        stats=_mapping(data["stats"], "data.stats"),
        raw=data,
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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def format_local_time(value: str | None) -> str:
    parsed = parse_timestamp(value)
    if parsed is None:
        return value or "unknown"
    return f"{parsed.astimezone(LOCAL_ZONE):%Y-%m-%d %H:%M} (São Paulo)"


def format_countdown(value: str | None, now: datetime | None = None) -> str:
    parsed = parse_timestamp(value)
    if parsed is None:
        return "unknown"
    now = now or utcnow()
    minutes = int(abs((parsed - now).total_seconds()) // 60)
    hours, minutes = divmod(minutes, 60)
    span = f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"
    return f"in {span}" if parsed >= now else f"overdue by {span}"


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


def describe_reset_quota(reset_type: str, text: str) -> str:
    lower = text.lower()
    if reset_type == "banked":
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
    if TEASER_RE.search(reset.text):
        return "FOLLOW-UP TEASED"
    return "RESET DEPLOYED"


def reset_action(reset: ResetSignal, event: str) -> str:
    if event == "BANKED RESET CREDITED":
        return "CHECK STATUS"
    if event == "FOLLOW-UP TEASED":
        return "SPEND NOW" if IMMINENT_RE.search(reset.text) else "USE SOME"
    if event == "RESET DEPLOYED":
        return "USE SOME"
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


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def cadence_probability(stats: dict[str, object]) -> int | None:
    """Read the published cadence as a percentage, capped below actionable."""
    elapsed = _number(stats.get("days_since_last"))
    average = _number(stats.get("avg_interval_days"))
    if elapsed is None or average is None or average <= 0:
        return None
    return round(CADENCE_CEILING * min(1.0, max(0.0, elapsed) / average))


def stage_for(probability: int) -> tuple[str, str]:
    if probability >= SPEND_NOW_PERCENT:
        return "spend-now", "SPEND NOW"
    if probability >= USE_SOME_PERCENT:
        return "use-some", "USE SOME"
    return "wait", "WAIT"


def schedule_reading(schedule: ScheduledReset, now: datetime) -> Forecast:
    """The monitor's own reading of a scheduled reset awaiting evidence.

    The API attaches no probability to a schedule, so the number is the
    monitor's: an explicit announcement outranks cadence, and it crosses into
    SPEND NOW as the landing time arrives. A schedule whose landing time has
    passed stays actionable, because the tracker keeps it until it observes
    the reset rather than treating the deadline as completion.
    """
    due = parse_timestamp(schedule.scheduled_for)
    if due is not None and due <= now:
        probability = SCHEDULE_DUE_PERCENT
        basis = "scheduled-due"
    else:
        imminent = due is not None and due - now <= timedelta(
            minutes=SCHEDULE_IMMINENT_MINUTES
        )
        probability = SCHEDULE_IMMINENT_PERCENT if imminent else SCHEDULE_AHEAD_PERCENT
        basis = "scheduled"
    stage, action = stage_for(probability)
    return Forecast(probability, basis, stage, action)


def forecast(snapshot: StatusSnapshot, now: datetime | None = None) -> Forecast:
    """One percentage for how likely usable quota is in hand or on the way.

    A reset announced within the last few hours is not a prediction, so it reads
    as certainty. A scheduled reset the tracker is still awaiting evidence for
    comes next, because an explicit announcement outranks a probabilistic watch
    and cadence alike. Otherwise a live published forecast wins, and cadence
    carries the reading when no forecast applies. A forecast is not live once
    it has expired, nor once a reset announced after it has superseded it.
    """
    now = now or utcnow()
    reset = snapshot.latest_reset
    announced = parse_timestamp(reset.announced_at) if reset else None
    if announced is not None and now - announced <= timedelta(hours=LANDED_RESET_HOURS):
        return Forecast(100, "reset", "landed", "USE SOME")

    schedule = snapshot.scheduled_reset
    schedule_announced = parse_timestamp(schedule.announced_at) if schedule else None
    schedule_superseded = (
        announced is not None
        and schedule_announced is not None
        and announced > schedule_announced
    )
    if schedule is not None and not schedule_superseded:
        return schedule_reading(schedule, now)

    watch = snapshot.active_watch
    if watch is not None:
        observed = parse_timestamp(watch.observed_at)
        expires = parse_timestamp(watch.expires_at)
        superseded = (
            announced is not None and observed is not None and announced > observed
        )
        expired = expires is not None and expires <= now
        if not superseded and not expired:
            chance = watch.reset_chance_percent
            if chance is None:
                chance = LEVEL_CHANCE[watch.level]
            return Forecast(chance, "watch", *stage_for(chance))

    baseline = cadence_probability(snapshot.stats)
    if baseline is None:
        return Forecast(0, "unknown", "wait", "WAIT")
    return Forecast(baseline, "cadence", *stage_for(baseline))


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


def schedule_signature(schedule: ScheduledReset | None) -> str | None:
    if schedule is None:
        return None
    stable = {
        "id": schedule.id,
        "status": schedule.status,
        "reset_type": schedule.reset_type,
        "announced_at": schedule.announced_at,
        "scheduled_for": schedule.scheduled_for,
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
        values.append(f"{days:g} day{'' if days == 1 else 's'} since last")
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
            f"Quota: {describe_reset_quota(reset.reset_type, reset.text)}",
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


def format_scheduled_alert(
    schedule: ScheduledReset, stats: dict[str, object], now: datetime | None = None
) -> tuple[str, str, str]:
    now = now or utcnow()
    reading = schedule_reading(schedule, now)
    event = "RESET DUE" if reading.basis == "scheduled-due" else "RESET SCHEDULED"
    body = "\n".join(
        [
            f"Event: {event}",
            f"Action: {reading.action}",
            f"Lands (São Paulo): {format_local_time(schedule.scheduled_for)}",
            f"Countdown: {format_countdown(schedule.scheduled_for, now)}",
            f"Scope: {describe_scope(schedule.text)}",
            f"Affected plans: {describe_plans(schedule.text)}",
            f"Quota: {describe_reset_quota(schedule.reset_type, schedule.text)}",
            f"Text: {display_text(schedule.text)}",
            f"History: {format_stats(stats)}",
            f"Source post: {source_link(schedule.source)}",
            f"Tracker: {TRACKER_URL} (unofficial; verify in Codex status)",
        ]
    )
    return event, reading.action, body


def format_forecast_cleared() -> tuple[str, str, str]:
    return (
        "RESET FORECAST CLEARED",
        "WAIT",
        "\n".join(
            [
                "Event: RESET FORECAST CLEARED",
                "Action: WAIT",
                "Forecast: no active forecast and no scheduled reset are published",
                "Scope: unknown",
                "Affected plans: unknown",
                "Quota: unknown",
                f"Tracker: {TRACKER_URL} (unofficial; verify in Codex status)",
            ]
        ),
    )


def format_status_report(snapshot: StatusSnapshot) -> str:
    prediction = forecast(snapshot)
    lines = [
        "Codex Resets status",
        f"Tracker: {TRACKER_URL} (unofficial)",
        f"Forecast: {prediction.probability}% [{prediction.action}]"
        f" from {BASIS_LABEL[prediction.basis]}",
    ]
    if snapshot.latest_reset is None:
        lines.append("Latest reset: none")
    else:
        event, action, body = format_reset_alert(snapshot.latest_reset, snapshot.stats)
        lines.extend([f"Latest reset: {event} [{action}]", body])
    if snapshot.scheduled_reset is None:
        lines.append("Scheduled reset: none")
    else:
        event, action, body = format_scheduled_alert(
            snapshot.scheduled_reset, snapshot.stats
        )
        lines.extend([f"Scheduled reset: {event} [{action}]", body])
    if snapshot.active_watch is None:
        lines.append("Active watch: none")
    else:
        event, action, body = format_watch_alert(snapshot.active_watch, snapshot.stats)
        lines.extend([f"Active watch: {event} [{action}]", body])
    return "\n".join(lines)


def format_age(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    return f"{minutes // 60}h {minutes % 60:02d}m ago"


def bar_tooltip(
    snapshot: StatusSnapshot,
    prediction: Forecast,
    cache_state: str,
    age_seconds: float,
    now: datetime,
) -> str:
    lines = [
        f"Codex reset chance {prediction.probability}% — {BASIS_LABEL[prediction.basis]}",
        f"Action: {prediction.action}",
    ]
    schedule = snapshot.scheduled_reset
    if schedule is not None:
        lines.extend(
            [
                f"Scheduled reset: {format_local_time(schedule.scheduled_for)}"
                f" ({format_countdown(schedule.scheduled_for, now)})",
                f"Said: {display_text(schedule.text, 240)}",
            ]
        )
    watch = snapshot.active_watch
    if watch is not None:
        chance = (
            f"{watch.reset_chance_percent}%"
            if watch.reset_chance_percent is not None
            else "unpublished"
        )
        lines.extend(
            [
                f"Forecast: {watch.level}; chance {chance}; window {watch.forecast_window}",
                f"Forecast expires: {format_local_time(watch.expires_at)}",
                f"Said: {display_text(watch.text, 240)}",
            ]
        )
    reset = snapshot.latest_reset
    if reset is not None:
        lines.extend(
            [
                f"Last reset: {classify_reset(reset)} · {format_local_time(reset.announced_at)}",
                f"Scope: {describe_scope(reset.text)} · plans: {describe_plans(reset.text)}",
            ]
        )
    lines.extend(
        [
            f"History: {format_stats(snapshot.stats)}",
            f"Checked {format_age(age_seconds)}"
            + (" — tracker unreachable since" if cache_state == "stale" else ""),
            f"{TRACKER_URL} (unofficial; verify in Codex)",
        ]
    )
    return "\n".join(lines)


def cached_snapshot(state: dict[str, object]) -> StatusSnapshot | None:
    data = state.get("snapshot")
    if not isinstance(data, dict):
        return None
    try:
        return parse_status(json.dumps({"data": data}).encode())
    except ValueError:
        return None


def bar_payload(state: dict[str, object], now: datetime | None = None) -> dict[str, object]:
    """Render the cached status as the one line the bar indicator reads."""
    now = now or utcnow()
    snapshot = cached_snapshot(state)
    raw_fetched = state.get("fetched_at")
    fetched = parse_timestamp(raw_fetched) if isinstance(raw_fetched, str) else None
    if snapshot is None or fetched is None:
        return {
            "state": "unavailable",
            "visible": False,
            "probability": 0,
            "basis": "unknown",
            "stage": "wait",
            "action": "WAIT",
            "tooltip": f"Codex reset monitor has no status yet.\n{TRACKER_URL} (unofficial)",
        }
    age_seconds = max(0.0, (now - fetched).total_seconds())
    cache_state = "fresh" if age_seconds < CACHE_FRESH_SECONDS else "stale"
    prediction = forecast(snapshot, now)
    return {
        "state": cache_state,
        "visible": prediction.stage != "wait",
        "probability": prediction.probability,
        "basis": prediction.basis,
        "stage": prediction.stage,
        "action": prediction.action,
        "tooltip": bar_tooltip(snapshot, prediction, cache_state, age_seconds, now),
    }


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
    return utcnow().isoformat()


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


def signal_state(snapshot: StatusSnapshot, prediction: Forecast) -> dict[str, object]:
    """What the monitor remembers about a status, to detect the next change."""
    return {
        "last_reset_id": snapshot.latest_reset.id if snapshot.latest_reset else None,
        "watch_signature": watch_signature(snapshot.active_watch),
        "scheduled_signature": schedule_signature(snapshot.scheduled_reset),
        "stage": prediction.stage,
    }


def pending_alerts(
    snapshot: StatusSnapshot, state: dict[str, object], prediction: Forecast
) -> list[tuple[str, str, str]]:
    """The alerts this status raises against what the state last recorded."""
    current_reset_id = snapshot.latest_reset.id if snapshot.latest_reset else None
    current_watch_signature = watch_signature(snapshot.active_watch)
    previous_schedule = state.get("scheduled_signature")
    schedule_changed = schedule_signature(snapshot.scheduled_reset) != previous_schedule
    watch_changed = (
        "watch_signature" in state and current_watch_signature != state["watch_signature"]
    )

    alerts: list[tuple[str, str, str]] = []
    if current_reset_id and current_reset_id != state.get("last_reset_id"):
        alerts.append(format_reset_alert(snapshot.latest_reset, snapshot.stats))

    # A scheduled reset is the stronger signal: the tracker replaces a
    # probabilistic watch with it when an announcement names a landing. The
    # signature is read without an "in state" guard, so a monitor whose cached
    # response already carried a schedule reports it once after it learns to.
    if schedule_changed and snapshot.scheduled_reset is not None:
        alerts.append(format_scheduled_alert(snapshot.scheduled_reset, snapshot.stats))

    if watch_changed and snapshot.active_watch is not None:
        alerts.append(format_watch_alert(snapshot.active_watch, snapshot.stats))

    # A signal that went away is worth one message, and only when nothing else
    # explains it: a reset that landed, or a schedule that replaced the watch,
    # already speaks for itself.
    pending_gone = (
        watch_changed
        and snapshot.active_watch is None
        and state["watch_signature"] is not None
    ) or (
        schedule_changed
        and snapshot.scheduled_reset is None
        and previous_schedule is not None
    )
    if (
        pending_gone
        and not alerts
        and snapshot.active_watch is None
        and snapshot.scheduled_reset is None
    ):
        alerts.append(format_forecast_cleared())

    # A forecast that grows within its band changes no signature, so the stage
    # carries the escalation on its own — unless an alert above already says it.
    escalated = STAGE_RANK.get(prediction.stage, 0) > STAGE_RANK.get(
        state.get("stage"), 0
    )
    if escalated and not alerts:
        if snapshot.active_watch is not None:
            alerts.append(format_watch_alert(snapshot.active_watch, snapshot.stats))
        elif snapshot.scheduled_reset is not None:
            alerts.append(format_scheduled_alert(snapshot.scheduled_reset, snapshot.stats))
    return alerts


def deliver_alerts(alerts: list[tuple[str, str, str]]) -> None:
    labels = ", ".join(alert[0] for alert in alerts)
    body = "\n\n".join(alert[2] for alert in alerts)
    critical = any(alert[1] in {"SPEND NOW", "USE SOME"} for alert in alerts)
    notify(f"Codex reset monitor: {labels}", body[:4000], critical=critical)
    print(f"notified: {labels}")


def run(show_latest: bool = False) -> int:
    state = load_state()
    saved_etag = state.get("etag")
    etag = saved_etag if isinstance(saved_etag, str) and not show_latest else None
    try:
        snapshot, response_etag = fetch_status(etag)
    except Exception as error:
        return record_failure(state, error)

    if snapshot is None:
        # The tracker's representation is unchanged, but this monitor's derived
        # state can still lag it: a cache written before the monitor tracked a
        # signal, or a stage that time alone has moved. Reconciling the cached
        # snapshot keeps those transitions from waiting on the next fetch.
        cached = cached_snapshot(state)
        if cached is not None:
            prediction = forecast(cached)
            alerts = pending_alerts(cached, state, prediction)
            if alerts:
                deliver_alerts(alerts)
            state.update(signal_state(cached, prediction))
        state["failures"] = 0
        state["last_checked_at"] = now_utc()
        state["fetched_at"] = now_utc()
        save_state(state)
        print("status unchanged (304)")
        return 0

    if show_latest:
        print(format_status_report(snapshot))
        return 0

    settled = {
        "api_initialized": True,
        "failures": 0,
        "last_checked_at": now_utc(),
        "fetched_at": now_utc(),
        "snapshot": snapshot.raw,
    }
    prediction = forecast(snapshot)
    if not state.get("api_initialized"):
        state.pop("initialized", None)
        state.pop("seen", None)
        state.update(settled)
        state.update(signal_state(snapshot, prediction))
        if response_etag:
            state["etag"] = response_etag
        save_state(state)
        print("seeded current Codex Resets status")
        return 0

    alerts = pending_alerts(snapshot, state, prediction)
    if alerts:
        deliver_alerts(alerts)

    state.update(settled)
    state.update(signal_state(snapshot, prediction))
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
    parser.add_argument(
        "--bar",
        action="store_true",
        help="print the cached status as one JSON line for a bar indicator",
    )
    args = parser.parse_args()
    if args.test_notification:
        notify("Codex reset monitor", "Notifications are working.", critical=True)
        return 0
    if args.bar:
        # Reads the cache the timer writes: a bar polls far more often than the
        # tracker should be asked anything.
        print(json.dumps(bar_payload(load_state()), separators=(",", ":")))
        return 0
    return run(args.show_latest)


if __name__ == "__main__":
    raise SystemExit(main())

# Tibo Codex reset monitor

A small Linux user service that checks the read-only Codex Resets status API
every five minutes, sends a desktop notification when a reset or forecast
changes, and feeds a bar indicator that shows how likely a reset is right now.
It uses only the Python standard library and `notify-send`; no X account,
SocialData account, or MCP connection is required.

The tracker is an unofficial public source. Its reset records can identify
regular or banked resets and whether the evidence was an X post or an observed
signal. When available, its active watch includes a forecast level, probability,
window, and expiry. Public signals do not establish that a particular account
has reset, so notifications preserve unknown plan and quota scope and tell you
to verify in Codex.

The monitor sends one request per timer run, uses the API's `ETag` for
conditional requests, and treats HTTP 304 as a successful no-change result.
The first API-backed run seeds the current status without notifying. Later
runs notify for a new reset record, a materially changed forecast, or a
forecast that has grown past the next actionable threshold.

## Forecast

Every status resolves to one percentage, so the same number drives the
notification and the bar. A reset announced within the last three hours is not a
prediction and reads as certainty, advising `USE SOME` — the same thing its own
notification says. Otherwise a published forecast carries the
number — its own percentage, or the percentage its level stands for — unless it
has expired or the reset it predicted has already been announced. With no
forecast in force, the reading falls back to how far into the published average
interval the current gap has run, capped below the actionable threshold: cadence
alone never asks you to spend.

The percentage names a stage, and the stage names an action: `SPEND NOW` at 75
and above, `USE SOME` at 50, and `WAIT` below that.

## Bar indicator

`--bar` prints the cached status as one JSON line — probability, stage, action,
and a tooltip — so a bar can poll every few seconds while the tracker is asked
only once per timer run. A cache the monitor has not refreshed for fifteen
minutes reports itself stale rather than passing for current.

`eww/codex-reset.yuck` and `eww/codex-reset.scss` are an [eww](https://elkowar.github.io/eww)
widget over that line. It is hidden below `USE SOME`, so its appearing is itself
the signal, and it names no colour: style the `codex-reset--use-some`,
`codex-reset--spend-now`, and `codex-reset--landed` classes from your own
palette. Include it and place the widget in a bar:

```lisp
(include ".../eww/codex-reset.yuck")
(codex-reset-indicator :icon ".../eww/assets/codex-resets.jpg")
```

## Install

On Arch Linux:

```sh
sudo pacman -S --needed python libnotify
make install
```

Test notifications and inspect the current tracker status with:

```sh
~/.local/bin/tibo-codex-reset-monitor --test-notification
~/.local/bin/tibo-codex-reset-monitor --show-latest
~/.local/bin/tibo-codex-reset-monitor --bar
```

Inspect the timer and logs with:

```sh
systemctl --user status tibo-codex-reset-monitor.timer
journalctl --user -u tibo-codex-reset-monitor.service -n 50 --no-pager
```

Remove every installed file, including monitor state, with:

```sh
make uninstall
```

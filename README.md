# Tibo Codex reset monitor

A small Linux user service that checks the read-only Codex Resets status API
every five minutes, sends a desktop notification when a reset, a scheduled
reset, or a forecast changes, keeps a small local history of every check, and
feeds a bar indicator that shows how certain a reset is right now. It uses only
the Python standard library and `notify-send`; no X account, SocialData
account, or MCP connection is required.

The tracker is an unofficial public source. Its reset records can identify
regular or banked resets and whether the evidence was an X post or an observed
signal. When available, its active watch includes a forecast level, probability,
window, and expiry, and a scheduled reset names a landing time while the reset
is still awaiting execution evidence. Public signals do not establish that a
particular account has reset, so notifications preserve unknown plan and quota
scope and tell you to verify in Codex.

The monitor sends one request per timer run, uses the API's `ETag` for
conditional requests, and treats HTTP 304 as a successful no-change result. A
304 still reconciles the cached status against the monitor's own records, so a
reading that moves with time alone — a scheduled landing arriving — is reported
without waiting for the tracker to update.
The first API-backed run seeds the current status without notifying. Later
runs notify for a new reset record, a reset announced or rescheduled as a
scheduled reset, a materially changed forecast, or a forecast that has grown
past the next actionable threshold.

## Forecast

Every status resolves to a percentage and a stage. The percentage is how
certain the reset is; the stage is what to do about it, which lets a reset that
is certain but has not landed read 100 without claiming it is already usable.

A reset announced within the last three hours reads 100 and advises `USE SOME` —
the same thing its own notification says — in the `landed` stage. A scheduled
reset comes next, because it is an explicit announcement awaiting execution
evidence and so outranks a probabilistic watch and cadence alike. It also reads
100: the tracker withholds a probability because it awaits evidence, not
because the reset is in doubt. Its stage carries the difference — `scheduled`
while the landing is ahead and `scheduled-due` once its time has passed without
evidence — so a certain-but-unlanded reset can be styled apart from a landed
one. The action follows that state: `USE SOME` while the landing is ahead,
`SPEND NOW` once it is due, and `CHECK STATUS` for a banked reset, which the
account applies rather than the quota refilling.

Otherwise a published forecast carries the number — its own percentage, or the
percentage its level stands for — unless it has expired or the reset it
predicted has already been announced. With no forecast in force, the reading
falls back to how far into the published average interval the current gap has
run, capped below the actionable threshold: cadence alone never asks you to
spend. There the percentage names the stage: `SPEND NOW` at 75 and above,
`USE SOME` at 50, and `WAIT` below that.

## History

Every check appends one JSON object to
`$XDG_CACHE_HOME/tibo-codex-reset-monitor/status-YYYY-MM-DD.jsonl`, one file per
UTC day. An unchanged check records `{"checked_at": …, "changed": false}`, a
changed one adds the whole `data` object the tracker returned, and a check that
could not read the tracker records
`{"checked_at": …, "failed": true, "error": …}`. Nothing is ever rewritten or pruned, so the timeline answers questions the
monitor does not ask yet — how long a schedule preceded its landing, how often
the tracker revised a forecast, and whether a quiet stretch was a tracker that
did not move, a tracker the monitor could not reach, or a monitor that had
stopped running. A failed write is reported and never stops the run it records.
The directory is systemd's `CacheDirectory` for the service, and `make
uninstall` removes it along with everything else.

## Bar indicator

`--bar` prints the cached status as one JSON line — probability, stage, action,
and a tooltip — so a bar can poll every few seconds while the tracker is asked
only once per timer run. A cache the monitor has not refreshed for fifteen
minutes reports itself stale rather than passing for current.

`eww/codex-reset.yuck` and `eww/codex-reset.scss` are an [eww](https://elkowar.github.io/eww)
widget over that line. It is hidden in the `wait` stage, so its appearing is
itself the signal, and it names no colour: style the `codex-reset--use-some`,
`codex-reset--spend-now`, `codex-reset--scheduled`, `codex-reset--scheduled-due`,
and `codex-reset--landed` classes from your own palette. A scheduled reset
reaches 100 before it lands, so `codex-reset--scheduled` is where "certain, not
yet usable" belongs, and `codex-reset--landed` is the one to make green.
Include it and place the widget in a bar:

```lisp
(include ".../eww/codex-reset.yuck")
(codex-reset-indicator :icon ".../eww/assets/codex-resets.png")
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

Inspect the recorded history with:

```sh
ls "${XDG_CACHE_HOME:-$HOME/.cache}/tibo-codex-reset-monitor/"
tail -n 1 "${XDG_CACHE_HOME:-$HOME/.cache}"/tibo-codex-reset-monitor/status-*.jsonl
```

Remove every installed file, including monitor state and history, with:

```sh
make uninstall
```

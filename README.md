# Tibo Codex reset monitor

A desktop notifier and bar indicator for Codex usage-limit resets, fed by the
public [Codex Resets](https://codex-resets.com/) tracker.

Codex usage resets are announced on X, or observed and forecast by an
unofficial tracker, and nothing tells you when one has landed or is about to.
Spending quota just before a reset wastes nothing; holding it back through one
wastes it. Watching the tracker by hand is easy to forget.

This monitor is a systemd user timer that asks the tracker's read-only status
API once every five minutes. It sends a desktop notification when a reset is
recorded, a reset is scheduled or rescheduled, or a forecast changes enough to
act on, and it keeps a local history of every check. A bar can read the cached
status as often as it likes without asking the tracker anything.

It uses only the Python standard library and `notify-send`, sends one
conditional request per run, and needs no account. Public signals do not prove
that your account has reset, so notifications keep plan and quota scope
unknown and tell you to verify in Codex.

## Install

On Arch Linux:

```sh
sudo pacman -S --needed python libnotify
make install
```

`make install` copies the script to `~/.local/bin/tibo-codex-reset-monitor`,
installs the user service and timer, and starts them. The first run records
the current status without notifying. `make uninstall` removes every installed
file, including state and history.

## Use

```sh
# confirm notifications reach the desktop
tibo-codex-reset-monitor --test-notification
# fetch and print the tracker's current status
tibo-codex-reset-monitor --show-latest
# the cached forecast as one JSON line, for a bar
tibo-codex-reset-monitor --bar
# timer state and recent runs
systemctl --user status tibo-codex-reset-monitor.timer
journalctl --user -u tibo-codex-reset-monitor.service -n 50 --no-pager
```

Every status resolves to a certainty percentage and a stage with an advised
action:

| Stage | Reads | Action |
|---|---|---|
| `landed` | a reset announced within the last three hours, 100 | `USE SOME` |
| `scheduled` | a reset announced for a time still ahead, 100 | `USE SOME` |
| `scheduled-due` | a scheduled time that has passed without evidence, 100 | `SPEND NOW` |
| `spend-now` | a published forecast of 75 or more | `SPEND NOW` |
| `use-some` | a published forecast of 50 or more | `USE SOME` |
| `wait` | anything lower | `WAIT` |

A scheduled banked reset advises `CHECK STATUS` instead, since the account
applies it rather than the quota refilling. Without a forecast in force, the
percentage comes from how far the current gap has run into the tracker's
average interval, capped below 50: cadence alone never asks you to spend.

Each check appends a JSON line to
`${XDG_CACHE_HOME:-~/.cache}/tibo-codex-reset-monitor/status-YYYY-MM-DD.jsonl`
(UTC days): unchanged, changed with the tracker's full data, or failed with
the error. Nothing is rewritten or pruned.

## Bar indicator

`eww/codex-reset.yuck` and `eww/codex-reset.scss` are an
[eww](https://elkowar.github.io/eww) widget over `--bar`. It is hidden in the
`wait` stage, so its appearance is the signal, and a cache older than fifteen
minutes shows its number marked stale. It names no colour: style
`codex-reset--<stage>` from your own palette, with `scheduled` for "certain,
not yet usable" and `landed` as the one to make green.

```lisp
(include ".../eww/codex-reset.yuck")
(codex-reset-indicator :icon ".../eww/assets/codex-resets.png")
```

## License

Public domain, under the [Unlicense](UNLICENSE).

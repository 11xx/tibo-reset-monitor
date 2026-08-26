# Tibo Codex reset monitor

A small Linux user service that checks the read-only Codex Resets status API
every 30 minutes and sends a desktop notification when a reset or forecast
changes. It uses only the Python standard library and `notify-send`; no X
account, SocialData account, or MCP connection is required.

The tracker is an unofficial public source. Its reset records can identify
regular or banked resets and whether the evidence was an X post or an observed
signal. When available, its active watch includes a forecast level, probability,
window, and expiry. Public signals do not establish that a particular account
has reset, so notifications preserve unknown plan and quota scope and tell you
to verify in Codex.

The monitor sends one request per timer run, uses the API's `ETag` for
conditional requests, and treats HTTP 304 as a successful no-change result.
The first API-backed run seeds the current status without notifying. Later
runs notify only for a new reset record or a materially changed forecast.

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

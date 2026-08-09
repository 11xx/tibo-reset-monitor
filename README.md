# Tibo Codex reset monitor

A small Linux user service that checks Tibo's public Nitter feed every five
minutes and sends a desktop notification for Codex usage-limit resets. It uses
only the Python standard library and `notify-send`; no X account is required.

## Install

On Arch Linux:

```sh
sudo pacman -S --needed python libnotify
make install
```

The first run records the 20 posts exposed by the RSS feed without notifying.
Later runs notify only for new matching posts.

Test notifications and inspect classifications with:

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

APP := tibo-codex-reset-monitor
BIN := $(HOME)/.local/bin/$(APP)
UNIT_DIR := $(HOME)/.config/systemd/user
STATE_DIR := $(HOME)/.local/state/$(APP)
CACHE_DIR := $(HOME)/.cache/$(APP)

.PHONY: install uninstall test

install:
	command -v python3 >/dev/null
	command -v notify-send >/dev/null
	install -Dm755 tibo_codex_reset_monitor.py "$(BIN)"
	install -Dm644 $(APP).service "$(UNIT_DIR)/$(APP).service"
	install -Dm644 $(APP).timer "$(UNIT_DIR)/$(APP).timer"
	systemctl --user daemon-reload
	systemctl --user enable --now $(APP).timer
	systemctl --user start $(APP).service

uninstall:
	-systemctl --user disable --now $(APP).timer
	rm -f "$(BIN)" "$(UNIT_DIR)/$(APP).service" "$(UNIT_DIR)/$(APP).timer"
	rm -f "$(STATE_DIR)/state.json" "$(STATE_DIR)/state.tmp"
	-rmdir "$(STATE_DIR)"
	rm -f "$(CACHE_DIR)"/status-*.jsonl
	-rmdir "$(CACHE_DIR)"
	systemctl --user daemon-reload

test:
	python3 -m unittest -v

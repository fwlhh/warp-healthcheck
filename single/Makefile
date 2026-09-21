SYSTEMD_DIR ?= /etc/systemd/system
BINDIR      ?= /usr/local/bin
SCRIPT      := warp-bot

.PHONY: install uninstall test

install:
	install -d $(BINDIR) $(SYSTEMD_DIR)
	install -m 0755 bin/$(SCRIPT).sh $(BINDIR)/$(SCRIPT).sh
	install -m 0644 etc/systemd/$(SCRIPT).service $(SYSTEMD_DIR)/$(SCRIPT).service
	@if [ ! -f /etc/$(SCRIPT).env ]; then \
	  install -m 0600 etc/$(SCRIPT).env.example /etc/$(SCRIPT).env; \
	  echo "created /etc/$(SCRIPT).env — edit it before starting"; \
	fi
	systemctl daemon-reload
	@echo "next: edit /etc/$(SCRIPT).env, then systemctl enable --now $(SCRIPT)"

uninstall:
	-systemctl stop $(SCRIPT) 2>/dev/null
	-systemctl disable $(SCRIPT) 2>/dev/null
	rm -f $(SYSTEMD_DIR)/$(SCRIPT).service
	rm -f $(BINDIR)/$(SCRIPT).sh
	systemctl daemon-reload

test:
	tests/run.sh
SYSTEMD_DIR ?= /etc/systemd/system
BINDIR      ?= /usr/local/bin

.PHONY: help \
        install-single uninstall-single \
        install-coordinator install-agent \
        uninstall-coordinator uninstall-agent \
        lint test test-single test-fleet

help:
	@echo "Targets:"
	@echo "  install-single           install single-node bash bot on this host"
	@echo "  uninstall-single         remove single-node bash bot"
	@echo "  install-coordinator      install fleet coordinator on this host"
	@echo "  install-agent            install fleet agent on this host"
	@echo "  uninstall-coordinator    remove fleet coordinator"
	@echo "  uninstall-agent          remove fleet agent"
	@echo "  lint                     run shellcheck and py_compile"
	@echo "  test                     run all tests"

# --- single ---------------------------------------------------------------
install-single:
	$(MAKE) -C single install

uninstall-single:
	$(MAKE) -C single uninstall

# --- fleet ----------------------------------------------------------------
install-coordinator:
	install -d $(BINDIR) $(SYSTEMD_DIR)
	install -m 0755 fleet/coordinator/coordinator.py $(BINDIR)/warp-coordinator
	install -m 0644 fleet/systemd/warp-coordinator.service $(SYSTEMD_DIR)/warp-coordinator.service
	@if [ ! -f /etc/warp-coordinator.env ]; then \
	  install -m 0600 fleet/coordinator/coordinator.env.example /etc/warp-coordinator.env; \
	  echo "created /etc/warp-coordinator.env — edit it before starting"; \
	fi
	systemctl daemon-reload
	@echo "next: edit /etc/warp-coordinator.env, then systemctl enable --now warp-coordinator"

uninstall-coordinator:
	-systemctl stop warp-coordinator 2>/dev/null
	-systemctl disable warp-coordinator 2>/dev/null
	rm -f $(SYSTEMD_DIR)/warp-coordinator.service
	rm -f $(BINDIR)/warp-coordinator
	systemctl daemon-reload

install-agent:
	install -d $(BINDIR) $(SYSTEMD_DIR)
	install -m 0755 fleet/agent/agent.py $(BINDIR)/warp-agent
	install -m 0644 fleet/systemd/warp-agent.service $(SYSTEMD_DIR)/warp-agent.service
	@if [ ! -f /etc/warp-agent.env ]; then \
	  install -m 0600 fleet/agent/agent.env.example /etc/warp-agent.env; \
	  echo "created /etc/warp-agent.env — edit it before starting"; \
	fi
	systemctl daemon-reload
	@echo "next: edit /etc/warp-agent.env, then systemctl enable --now warp-agent"

uninstall-agent:
	-systemctl stop warp-agent 2>/dev/null
	-systemctl disable warp-agent 2>/dev/null
	rm -f $(SYSTEMD_DIR)/warp-agent.service
	rm -f $(BINDIR)/warp-agent
	systemctl daemon-reload

# --- dev ------------------------------------------------------------------
lint:
	shellcheck single/bin/*.sh single/scripts/*.sh \
	          single/tests/run.sh single/tests/cases/*.sh
	python3 -m py_compile fleet/coordinator/coordinator.py
	python3 -m py_compile fleet/agent/agent.py

test-single:
	$(MAKE) -C single test

test-fleet:
	fleet/tests/run.sh

test: test-single test-fleet